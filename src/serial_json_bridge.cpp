#include "serial_json_bridge.h"

#include <ArduinoJson.h>
#include <mbedtls/base64.h>

#include <inttypes.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>

namespace lpwa {

namespace {
#ifndef LPWA_ENABLE_TRACE_TELEMETRY
#define LPWA_ENABLE_TRACE_TELEMETRY 0
#endif
constexpr uint8_t kTraceTelemetryTtl = 3;
constexpr uint32_t kTraceTelemetryMinIntervalMs = 120;
constexpr uint16_t kPingProbeMagic = 0x504C;  // 'LP'
constexpr uint8_t kPingProbeVersion = 1;
constexpr uint8_t kPingProbeKindRequest = 1;
constexpr uint8_t kPingProbeKindPongOk = 2;
constexpr uint8_t kPingProbeKindPongBad = 3;
constexpr uint16_t kPingProbeBytesDefault = 1000;
constexpr uint16_t kPingProbeBytesMax = 1000;
#if LPWA_ENABLE_WIFI_LR && (!LPWA_ENABLE_BLE_RELAY || LPWA_ALLOW_WIFI_LR_WITH_BLE) && defined(WIFI_PROTOCOL_LR)
constexpr bool kLongRangeProfileAvailable = true;
#else
constexpr bool kLongRangeProfileAvailable = false;
#endif

struct PingProbeHeader {
  uint16_t magic;
  uint8_t version;
  uint8_t kind;
  uint16_t seq;
  uint32_t tag;
  uint32_t txMs;
  uint16_t payloadLen;
  uint32_t payloadHash;
} __attribute__((packed));
static_assert(sizeof(PingProbeHeader) == 20, "PingProbeHeader size mismatch");

bool isBroadcastTarget(const char* dst) {
  if (dst == nullptr || dst[0] == '\0') {
    return true;
  }
  if (std::strcmp(dst, "*") == 0) {
    return true;
  }
  return std::strcmp(dst, "all") == 0;
}

bool equalsIgnoreCase(const String& a, const char* b) {
  if (b == nullptr) {
    return false;
  }
  String rhs(b);
  return a.equalsIgnoreCase(rhs);
}

bool isSelfTarget(const String& selfNodeId, const char* dst) {
  if (isBroadcastTarget(dst)) {
    return true;
  }
  return equalsIgnoreCase(selfNodeId, dst);
}

bool parseNodeIdText(const char* text, uint32_t* outNodeId) {
  if (outNodeId != nullptr) {
    *outNodeId = 0;
  }
  if (text == nullptr || text[0] == '\0') {
    return false;
  }

  const size_t len = std::strlen(text);
  if (len != 10 || text[0] != '0' || (text[1] != 'x' && text[1] != 'X')) {
    return false;
  }
  for (size_t i = 2; i < 10; ++i) {
    const char ch = text[i];
    const bool isDigit = (ch >= '0' && ch <= '9');
    const bool isLowerHex = (ch >= 'a' && ch <= 'f');
    const bool isUpperHex = (ch >= 'A' && ch <= 'F');
    if (!isDigit && !isLowerHex && !isUpperHex) {
      return false;
    }
  }

  char* endPtr = nullptr;
  const unsigned long parsed = std::strtoul(text + 2, &endPtr, 16);
  if (endPtr == (text + 2) || (endPtr != nullptr && *endPtr != '\0') || parsed == 0UL) {
    return false;
  }
  if (outNodeId != nullptr) {
    *outNodeId = static_cast<uint32_t>(parsed);
  }
  return true;
}

bool parseHexU32(const char* text, uint32_t* outValue) {
  if (outValue != nullptr) {
    *outValue = 0;
  }
  if (text == nullptr || text[0] == '\0') {
    return false;
  }
  const size_t len = std::strlen(text);
  if (len > 8) {
    return false;
  }
  for (size_t i = 0; i < len; ++i) {
    const char ch = text[i];
    const bool isDigit = (ch >= '0' && ch <= '9');
    const bool isLowerHex = (ch >= 'a' && ch <= 'f');
    const bool isUpperHex = (ch >= 'A' && ch <= 'F');
    if (!isDigit && !isLowerHex && !isUpperHex) {
      return false;
    }
  }
  char* endPtr = nullptr;
  const unsigned long parsed = std::strtoul(text, &endPtr, 16);
  if (endPtr == text || (endPtr != nullptr && *endPtr != '\0')) {
    return false;
  }
  if (outValue != nullptr) {
    *outValue = static_cast<uint32_t>(parsed);
  }
  return true;
}

uint32_t fnv1a32(const uint8_t* data, size_t len) {
  uint32_t hash = 2166136261UL;
  for (size_t i = 0; i < len; ++i) {
    hash ^= static_cast<uint32_t>(data[i]);
    hash *= 16777619UL;
  }
  return hash;
}

void fillPingProbePayload(uint8_t* outBuffer, size_t len, uint32_t seed) {
  if (outBuffer == nullptr || len == 0) {
    return;
  }
  uint32_t x = seed ^ 0xA5A5A5A5UL;
  for (size_t i = 0; i < len; ++i) {
    x = x * 1664525UL + 1013904223UL;
    outBuffer[i] = static_cast<uint8_t>((x >> 24) & 0xFF);
  }
}

size_t encodePingProbePacket(uint8_t* outPacket, size_t outCapacity, const PingProbeHeader& header,
                             const uint8_t* payload) {
  const size_t packetLen = sizeof(PingProbeHeader) + static_cast<size_t>(header.payloadLen);
  if (outPacket == nullptr || outCapacity < packetLen || packetLen > kMaxAppPayload) {
    return 0;
  }
  if (header.payloadLen > 0 && payload == nullptr) {
    return 0;
  }
  std::memcpy(outPacket, &header, sizeof(PingProbeHeader));
  if (header.payloadLen > 0) {
    std::memcpy(outPacket + sizeof(PingProbeHeader), payload, header.payloadLen);
  }
  return packetLen;
}

bool decodePingProbePacket(const uint8_t* packet, size_t packetLen, PingProbeHeader* outHeader,
                           const uint8_t** outPayload) {
  if (packet == nullptr || outHeader == nullptr || packetLen < sizeof(PingProbeHeader)) {
    return false;
  }
  std::memcpy(outHeader, packet, sizeof(PingProbeHeader));
  if (outHeader->magic != kPingProbeMagic || outHeader->version != kPingProbeVersion) {
    return false;
  }
  if (outHeader->kind != kPingProbeKindRequest && outHeader->kind != kPingProbeKindPongOk &&
      outHeader->kind != kPingProbeKindPongBad) {
    return false;
  }
  if (outHeader->payloadLen > kPingProbeBytesMax) {
    return false;
  }
  const size_t expectedLen = sizeof(PingProbeHeader) + static_cast<size_t>(outHeader->payloadLen);
  if (packetLen != expectedLen) {
    return false;
  }
  if (outPayload != nullptr) {
    *outPayload = packet + sizeof(PingProbeHeader);
  }
  return true;
}

String formatHex8(uint32_t value) {
  char buffer[9];
  std::snprintf(buffer, sizeof(buffer), "%08" PRIx32, value);
  return String(buffer);
}

const char* radioProfileName(EspNowMesh::RadioProfile profile) {
  switch (profile) {
    case EspNowMesh::RadioProfile::Balanced:
      return "balanced";
    case EspNowMesh::RadioProfile::LongRange:
      return "long_range";
    case EspNowMesh::RadioProfile::Coexist:
      return "coexist";
    default:
      return "unknown";
  }
}

bool parseRadioProfile(const String& text, EspNowMesh::RadioProfile* outProfile) {
  if (outProfile != nullptr) {
    *outProfile = EspNowMesh::RadioProfile::Balanced;
  }
  if (text.equalsIgnoreCase("balanced") || text.equalsIgnoreCase("normal")) {
    return true;
  }
  if (text.equalsIgnoreCase("long_range") || text.equalsIgnoreCase("lr")) {
    if (outProfile != nullptr) {
      *outProfile = EspNowMesh::RadioProfile::LongRange;
    }
    return true;
  }
  if (text.equalsIgnoreCase("coexist") || text.equalsIgnoreCase("ble")) {
    if (outProfile != nullptr) {
      *outProfile = EspNowMesh::RadioProfile::Coexist;
    }
    return true;
  }
  return false;
}

}  // namespace

SerialJsonBridge::SerialJsonBridge(EspNowMesh* mesh, BleRelay* ble) : mesh_(mesh), ble_(ble) {}

void SerialJsonBridge::begin(Stream* stream) {
  serial_ = stream;
  lineLength_ = 0;
  droppingInputLine_ = false;
  bridgeStats_ = BridgeStats{};
  lastTraceTelemetryMs_ = 0;

  if (serial_ == nullptr) {
    return;
  }

  StaticJsonDocument<256> doc;
  doc["event"] = "bridge_ready";
  doc["type"] = "bridge_ready";
  if (mesh_ != nullptr) {
    doc["node_id"] = formatNodeId(mesh_->nodeId());
    JsonObject caps = doc.createNestedObject("caps");
    caps["directed_unicast"] = true;
    caps["route_stats"] = true;
    caps["route_list"] = true;
    caps["routing_mode"] = LPWA_ROUTING_MODE;
    caps["reliable_1k"] = true;
    caps["reliable_profiles"] = "0:25+8,1:25+10";
    caps["radio_profile"] = true;
    caps["nodeinfo_cfg"] = true;
  }
  serializeJson(doc, *serial_);
  serial_->println();
}

void SerialJsonBridge::loop() {
  if (serial_ != nullptr) {
    while (serial_->available() > 0) {
      const char ch = static_cast<char>(serial_->read());
      if (ch == '\r') {
        continue;
      }
      if (ch == '\n') {
        if (droppingInputLine_) {
          droppingInputLine_ = false;
          lineLength_ = 0;
          continue;
        }
        if (lineLength_ > 0) {
          lineBuffer_[lineLength_] = '\0';
          handleLine(lineBuffer_);
          lineLength_ = 0;
        }
        continue;
      }

      if (droppingInputLine_) {
        continue;
      }
      if (lineLength_ >= (kLineBufferSize - 1)) {
        lineLength_ = 0;
        droppingInputLine_ = true;
        bridgeStats_.commandErrors++;
        emitError("line_too_long", "max 4095 bytes");
        continue;
      }
      lineBuffer_[lineLength_++] = ch;
    }
  }

  if (mesh_ != nullptr) {
    ReassembledMessage message{};
    uint8_t drained = 0;
    while (drained < kMaxMeshDrainPerLoop && mesh_->popReceivedMessage(&message)) {
      emitMeshMessage(message);
      drained++;
    }
  }

  if (ble_ != nullptr) {
    BleRelayMessage message{};
    uint8_t drained = 0;
    while (drained < kMaxBleDrainPerLoop && ble_->popReceived(&message)) {
      emitBleMessage(message);
      drained++;
    }
  }
}

void SerialJsonBridge::handleLine(const char* line) {
  if (line == nullptr || serial_ == nullptr) {
    return;
  }

  bridgeStats_.commandCount++;

  DynamicJsonDocument request(3072);
  const DeserializationError err = deserializeJson(request, line);
  if (err) {
    bridgeStats_.commandErrors++;
    char detail[96];
    std::snprintf(detail, sizeof(detail), "%s len=%u", err.c_str(), static_cast<unsigned>(std::strlen(line)));
    emitError("json_parse_error", detail);
    return;
  }

  const String selfNodeId = (mesh_ != nullptr) ? formatNodeId(mesh_->nodeId()) : String("0x00000000");
  String cmd = request["cmd"] | "";
  String type = request["type"] | "";

  if (cmd.isEmpty() && !type.isEmpty()) {
    String via = request["via"] | "wifi";
    uint8_t ttl = request["ttl"] | kDefaultTtl;
    if (ttl == 0) {
      ttl = 1;
    }

    if (type == "nodes_request") {
      cmd = "get_nodes";
    } else if (type == "routes_request") {
      cmd = "get_routes";
    } else if (type == "chat" || type == "ping" || type == "long_text_start" || type == "long_text_chunk" ||
               type == "long_text_end" || type == "reliable_1k_start" || type == "reliable_1k_chunk" ||
               type == "reliable_1k_end" || type == "reliable_1k_nack" || type == "reliable_1k_repair" ||
               type == "reliable_1k_result") {
      if (via == "ble") {
        if (type != "chat") {
          bridgeStats_.commandErrors++;
          emitError("ble_unsupported", "BLE supports chat only");
          return;
        }
        String text = request["text"] | "";
        if (text.isEmpty()) {
          bridgeStats_.commandErrors++;
          emitError("missing_field", "text");
          return;
        }
        if (text.length() > kBleRelayTextMax) {
          bridgeStats_.commandErrors++;
          emitError("payload_too_large", "BLE text max 16 bytes");
          return;
        }
        uint32_t messageId = 0;
        const bool ok = (ble_ != nullptr) && ble_->sendText(text.c_str(), ttl, &messageId);
        if (ok) {
          bridgeStats_.sentText++;
        }
        emitAck(type.c_str(), ok, "ble", messageId);
        return;
      }

      if (via != "wifi") {
        bridgeStats_.commandErrors++;
        emitError("invalid_field", "via");
        return;
      }

      DynamicJsonDocument envelope(1400);
      envelope["app"] = "lpwa";
      envelope["type"] = type;
      envelope["src"] = selfNodeId;
      envelope["ttl"] = ttl;
      const char* dst = request["dst"] | "";
      uint32_t dstNodeId = 0;
      const bool hasDirectedDst = !isBroadcastTarget(dst);
      if (hasDirectedDst && !parseNodeIdText(dst, &dstNodeId)) {
        bridgeStats_.commandErrors++;
        emitError("invalid_field", "dst must be 0xXXXXXXXX");
        return;
      }
      if (!isBroadcastTarget(dst)) {
        envelope["dst"] = dst;
      }
      if (request.containsKey("need_ack")) {
        envelope["need_ack"] = request["need_ack"] | false;
      }
      if (request.containsKey("retry_no")) {
        envelope["retry_no"] = request["retry_no"] | 0;
      }
      const char* e2eId = request["e2e_id"] | "";
      if (e2eId[0] != '\0') {
        envelope["e2e_id"] = e2eId;
      }

      if (type == "chat") {
        String text = request["text"] | "";
        if (text.isEmpty()) {
          bridgeStats_.commandErrors++;
          emitError("missing_field", "text");
          return;
        }
        envelope["text"] = text;
      } else if (type == "ping") {
        envelope["seq"] = request["seq"] | 0;
        envelope["ping_id"] = request["ping_id"] | "";
        envelope["ts_ms"] = request["ts_ms"] | millis();
      } else if (type == "long_text_start") {
        const String textId = request["text_id"] | "";
        const int size = request["size"] | -1;
        const int chunks = request["chunks"] | -1;
        if (textId.isEmpty() || size < 0 || chunks < 0) {
          bridgeStats_.commandErrors++;
          emitError("invalid_long_text_start", "text_id/size/chunks required");
          return;
        }
        envelope["type"] = "lt_s";
        envelope["id"] = textId;
        envelope["e"] = request["encoding"] | "utf-8";
        envelope["z"] = size;
        envelope["n"] = chunks;
      } else if (type == "long_text_chunk") {
        const String textId = request["text_id"] | "";
        const int index = request["index"] | -1;
        const String dataB64 = request["data_b64"] | "";
        if (textId.isEmpty() || index < 0 || dataB64.isEmpty()) {
          bridgeStats_.commandErrors++;
          emitError("invalid_long_text_chunk", "text_id/index/data_b64 required");
          return;
        }
        envelope["type"] = "lt_c";
        envelope["id"] = textId;
        envelope["i"] = index;
        envelope["d"] = dataB64;
      } else if (type == "long_text_end") {
        const String textId = request["text_id"] | "";
        if (textId.isEmpty()) {
          bridgeStats_.commandErrors++;
          emitError("invalid_long_text_end", "text_id required");
          return;
        }
        envelope["type"] = "lt_e";
        envelope["id"] = textId;
        if (request.containsKey("encoding")) {
          envelope["e"] = request["encoding"] | "utf-8";
        }
        if (request.containsKey("size")) {
          envelope["z"] = request["size"] | 0;
        }
        if (request.containsKey("chunks")) {
          envelope["n"] = request["chunks"] | 0;
        }
        const char* hash = request["sha256"] | "";
        if (hash != nullptr && hash[0] != '\0') {
          envelope["h"] = hash;
        }
      } else if (type == "reliable_1k_start") {
        const String r1kId = request["r1k_id"] | "";
        const int profileId = request["profile_id"] | -1;
        const int dataShards = request["data_shards"] | -1;
        const int parityShards = request["parity_shards"] | -1;
        const int shardSize = request["shard_size"] | -1;
        const int size = request["size"] | -1;
        if (r1kId.isEmpty() || profileId < 0 || dataShards <= 0 || parityShards < 0 || shardSize <= 0 || size < 0) {
          bridgeStats_.commandErrors++;
          emitError("invalid_reliable_1k_start", "r1k_id/profile_id/data_shards/parity_shards/shard_size/size required");
          return;
        }
        envelope["type"] = "r1k_s";
        envelope["id"] = r1kId;
        envelope["v"] = request["version"] | 1;
        envelope["pf"] = profileId;
        envelope["k"] = dataShards;
        envelope["m"] = parityShards;
        envelope["s"] = shardSize;
        envelope["z"] = size;
        const char* crc = request["crc32"] | "";
        if (crc != nullptr && crc[0] != '\0') {
          envelope["c"] = crc;
        }
        const char* sha = request["sha256"] | "";
        if (sha != nullptr && sha[0] != '\0') {
          envelope["h"] = sha;
        }
      } else if (type == "reliable_1k_chunk") {
        const String r1kId = request["r1k_id"] | "";
        const int index = request["index"] | -1;
        const String dataB64 = request["data_b64"] | "";
        if (r1kId.isEmpty() || index < 0 || dataB64.isEmpty()) {
          bridgeStats_.commandErrors++;
          emitError("invalid_reliable_1k_chunk", "r1k_id/index/data_b64 required");
          return;
        }
        envelope["type"] = "r1k_d";
        envelope["id"] = r1kId;
        envelope["i"] = index;
        envelope["d"] = dataB64;
      } else if (type == "reliable_1k_end") {
        const String r1kId = request["r1k_id"] | "";
        if (r1kId.isEmpty()) {
          bridgeStats_.commandErrors++;
          emitError("invalid_reliable_1k_end", "r1k_id required");
          return;
        }
        envelope["type"] = "r1k_e";
        envelope["id"] = r1kId;
        if (request.containsKey("size")) {
          envelope["z"] = request["size"] | 0;
        }
        if (request.containsKey("data_shards")) {
          envelope["k"] = request["data_shards"] | 0;
        }
        if (request.containsKey("parity_shards")) {
          envelope["m"] = request["parity_shards"] | 0;
        }
        if (request.containsKey("shard_size")) {
          envelope["s"] = request["shard_size"] | 0;
        }
        const char* crc = request["crc32"] | "";
        if (crc != nullptr && crc[0] != '\0') {
          envelope["c"] = crc;
        }
        const char* sha = request["sha256"] | "";
        if (sha != nullptr && sha[0] != '\0') {
          envelope["h"] = sha;
        }
      } else if (type == "reliable_1k_nack") {
        const String r1kId = request["r1k_id"] | "";
        if (r1kId.isEmpty() || !request.containsKey("missing")) {
          bridgeStats_.commandErrors++;
          emitError("invalid_reliable_1k_nack", "r1k_id/missing required");
          return;
        }
        envelope["type"] = "r1k_n";
        envelope["id"] = r1kId;
        envelope["missing"] = request["missing"];
      } else if (type == "reliable_1k_repair") {
        const String r1kId = request["r1k_id"] | "";
        const int index = request["index"] | -1;
        const String dataB64 = request["data_b64"] | "";
        if (r1kId.isEmpty() || index < 0 || dataB64.isEmpty()) {
          bridgeStats_.commandErrors++;
          emitError("invalid_reliable_1k_repair", "r1k_id/index/data_b64 required");
          return;
        }
        envelope["type"] = "r1k_r";
        envelope["id"] = r1kId;
        envelope["i"] = index;
        envelope["d"] = dataB64;
      } else if (type == "reliable_1k_result") {
        const String r1kId = request["r1k_id"] | "";
        const String status = request["status"] | "";
        if (r1kId.isEmpty() || status.isEmpty()) {
          bridgeStats_.commandErrors++;
          emitError("invalid_reliable_1k_result", "r1k_id/status required");
          return;
        }
        envelope["type"] = "r1k_o";
        envelope["id"] = r1kId;
        envelope["status"] = status;
        if (request.containsKey("missing")) {
          envelope["missing"] = request["missing"];
        }
        if (request.containsKey("recovered")) {
          envelope["recovered"] = request["recovered"] | 0;
        }
        if (request.containsKey("latency_ms")) {
          envelope["latency_ms"] = request["latency_ms"] | 0;
        }
      }

      String wireText;
      serializeJson(envelope, wireText);
      if (wireText.length() > kMaxAppPayload) {
        bridgeStats_.commandErrors++;
        emitError("payload_too_large", "type envelope exceeds 1024 bytes");
        return;
      }

      uint32_t messageId = 0;
      bool ok = false;
      if (mesh_ != nullptr) {
        if (hasDirectedDst) {
          ok = mesh_->sendTextDirected(wireText.c_str(), dstNodeId, ttl, &messageId);
        } else {
          ok = mesh_->sendText(wireText.c_str(), ttl, &messageId);
        }
      }

      if (ok) {
        if (type == "chat" || type == "ping" || type == "long_text_start" || type == "long_text_chunk" ||
            type == "long_text_end" || type == "reliable_1k_start" || type == "reliable_1k_chunk" ||
            type == "reliable_1k_end" || type == "reliable_1k_nack" || type == "reliable_1k_repair" ||
            type == "reliable_1k_result") {
          bridgeStats_.sentText++;
          if (type == "reliable_1k_start" || type == "reliable_1k_chunk" || type == "reliable_1k_end" ||
              type == "reliable_1k_nack" || type == "reliable_1k_repair" || type == "reliable_1k_result") {
            bridgeStats_.sentReliable++;
          }
        }
      }
      emitAck(type.c_str(), ok, via.c_str(), messageId);
      return;
    } else {
      bridgeStats_.commandErrors++;
      emitError("unknown_type", type.c_str());
      return;
    }
  }

  if (cmd == "send_text") {
    const char* text = request["text"] | nullptr;
    if (text == nullptr) {
      bridgeStats_.commandErrors++;
      emitError("missing_field", "text");
      return;
    }

    const uint8_t ttl = request["ttl"] | kDefaultTtl;
    String via = request["via"] | "wifi";
    const char* dst = request["dst"] | "";
    uint32_t dstNodeId = 0;
    const bool hasDirectedDst = !isBroadcastTarget(dst);
    if (hasDirectedDst && !parseNodeIdText(dst, &dstNodeId)) {
      bridgeStats_.commandErrors++;
      emitError("invalid_field", "dst must be 0xXXXXXXXX");
      return;
    }

    uint32_t messageId = 0;
    bool ok = false;
    if (via == "wifi") {
      if (mesh_ != nullptr) {
        if (hasDirectedDst) {
          ok = mesh_->sendTextDirected(text, dstNodeId, ttl, &messageId);
        } else {
          ok = mesh_->sendText(text, ttl, &messageId);
        }
      }
    } else if (via == "ble") {
      if (std::strlen(text) > kBleRelayTextMax) {
        bridgeStats_.commandErrors++;
        emitError("payload_too_large", "BLE text max 16 bytes");
        return;
      }
      ok = (ble_ != nullptr) && ble_->sendText(text, ttl, &messageId);
    } else {
      bridgeStats_.commandErrors++;
      emitError("invalid_field", "via");
      return;
    }

    if (ok) {
      bridgeStats_.sentText++;
    }
    emitAck("send_text", ok, via.c_str(), messageId);
    return;
  }

  if (cmd == "set_radio_profile") {
    if (mesh_ == nullptr) {
      bridgeStats_.commandErrors++;
      emitError("mesh_unavailable", "mesh not initialized");
      return;
    }
    String profileText = request["profile"] | "";
    EspNowMesh::RadioProfile profile = EspNowMesh::RadioProfile::Balanced;
    if (profileText.isEmpty() || !parseRadioProfile(profileText, &profile)) {
      bridgeStats_.commandErrors++;
      emitError("invalid_field", "profile must be balanced/long_range/coexist");
      return;
    }
    if (profile == EspNowMesh::RadioProfile::LongRange && !kLongRangeProfileAvailable) {
      bridgeStats_.commandErrors++;
      emitError("unsupported_profile", "long_range is disabled by build flags");
      emitAck("set_radio_profile", false, "wifi", 0);
      return;
    }
    const bool ok = mesh_->setRadioProfile(profile);
    emitAck("set_radio_profile", ok, "wifi", 0);
    return;
  }

  if (cmd == "set_nodeinfo_cfg") {
    if (mesh_ == nullptr) {
      bridgeStats_.commandErrors++;
      emitError("mesh_unavailable", "mesh not initialized");
      return;
    }
    uint8_t ttl = request["ttl"] | kDefaultNodeInfoTtl;
    if (ttl == 0) {
      ttl = 1;
    }
    uint32_t periodMs = request["period_ms"] | kNodeInfoPeriodMs;
    mesh_->setNodeInfoConfig(ttl, periodMs);
    emitAck("set_nodeinfo_cfg", true, "wifi", 0);
    return;
  }

  if (cmd == "get_radio_profile") {
    if (mesh_ == nullptr) {
      bridgeStats_.commandErrors++;
      emitError("mesh_unavailable", "mesh not initialized");
      return;
    }
    DynamicJsonDocument out(256);
    out["event"] = "radio_profile";
    out["type"] = "radio_profile";
    out["profile"] = radioProfileName(mesh_->radioProfile());
    serializeJson(out, *serial_);
    serial_->println();
    return;
  }

  if (cmd == "send_binary") {
    const char* b64 = request["data_b64"] | nullptr;
    if (b64 == nullptr) {
      bridgeStats_.commandErrors++;
      emitError("missing_field", "data_b64");
      return;
    }

    String via = request["via"] | "wifi";
    if (via != "wifi") {
      bridgeStats_.commandErrors++;
      emitError("invalid_field", "send_binary supports wifi only");
      return;
    }

    uint8_t decoded[kMaxAppPayload];
    size_t decodedLen = 0;
    if (!decodeBase64(b64, decoded, sizeof(decoded), &decodedLen)) {
      bridgeStats_.commandErrors++;
      emitError("invalid_field", "data_b64");
      return;
    }

    const uint8_t ttl = request["ttl"] | kDefaultTtl;
    uint32_t messageId = 0;
    const char* dst = request["dst"] | "";
    uint32_t dstNodeId = 0;
    const bool hasDirectedDst = !isBroadcastTarget(dst);
    if (hasDirectedDst && !parseNodeIdText(dst, &dstNodeId)) {
      bridgeStats_.commandErrors++;
      emitError("invalid_field", "dst must be 0xXXXXXXXX");
      return;
    }
    bool ok = false;
    if (mesh_ != nullptr) {
      if (hasDirectedDst) {
        ok = mesh_->sendBinaryDirected(decoded, decodedLen, dstNodeId, ttl, &messageId);
      } else {
        ok = mesh_->sendBinary(decoded, decodedLen, ttl, &messageId);
      }
    }
    if (ok) {
      bridgeStats_.sentBinary++;
    }
    emitAck("send_binary", ok, "wifi", messageId);
    return;
  }

  if (cmd == "ping_probe") {
    String via = request["via"] | "wifi";
    if (via != "wifi") {
      bridgeStats_.commandErrors++;
      emitError("invalid_field", "ping_probe supports wifi only");
      return;
    }

    uint8_t ttl = request["ttl"] | kDefaultTtl;
    if (ttl == 0) {
      ttl = 1;
    }
    const char* dst = request["dst"] | "";
    uint32_t dstNodeId = 0;
    const bool hasDirectedDst = !isBroadcastTarget(dst);
    if (hasDirectedDst && !parseNodeIdText(dst, &dstNodeId)) {
      bridgeStats_.commandErrors++;
      emitError("invalid_field", "dst must be 0xXXXXXXXX");
      return;
    }

    uint16_t probeBytes = request["probe_bytes"] | kPingProbeBytesDefault;
    if (probeBytes == 0 || probeBytes > kPingProbeBytesMax) {
      bridgeStats_.commandErrors++;
      emitError("invalid_field", "probe_bytes must be 1..1000");
      return;
    }

    const uint16_t seq = static_cast<uint16_t>(request["seq"] | 0);
    uint32_t tag = 0;
    const char* pingIdText = request["ping_id"] | "";
    if (!parseHexU32(pingIdText, &tag)) {
      tag = (static_cast<uint32_t>(millis()) << 8) ^ static_cast<uint32_t>(seq);
    }
    const uint32_t txMs = request["ts_ms"] | millis();

    uint8_t probePayload[kPingProbeBytesMax];
    fillPingProbePayload(probePayload, probeBytes, tag ^ (static_cast<uint32_t>(seq) << 16) ^ txMs);
    const uint32_t probeHash = fnv1a32(probePayload, probeBytes);

    PingProbeHeader header{};
    header.magic = kPingProbeMagic;
    header.version = kPingProbeVersion;
    header.kind = kPingProbeKindRequest;
    header.seq = seq;
    header.tag = tag;
    header.txMs = txMs;
    header.payloadLen = probeBytes;
    header.payloadHash = probeHash;

    uint8_t packet[sizeof(PingProbeHeader) + kPingProbeBytesMax];
    const size_t packetLen = encodePingProbePacket(packet, sizeof(packet), header, probePayload);
    if (packetLen == 0) {
      bridgeStats_.commandErrors++;
      emitError("internal_error", "ping_probe_packet_encode_failed");
      return;
    }

    uint32_t messageId = 0;
    bool ok = false;
    if (mesh_ != nullptr) {
      if (hasDirectedDst) {
        ok = mesh_->sendBinaryDirected(packet, packetLen, dstNodeId, ttl, &messageId);
      } else {
        ok = mesh_->sendBinary(packet, packetLen, ttl, &messageId);
      }
    }
    if (ok) {
      bridgeStats_.sentBinary++;
    }
    emitAck("ping_probe", ok, "wifi", messageId);
    return;
  }

  if (cmd == "get_stats") {
    DynamicJsonDocument out(1800);
    out["event"] = "stats";
    out["type"] = "stats";

    JsonObject bridgeObj = out.createNestedObject("bridge");
    bridgeObj["commands"] = bridgeStats_.commandCount;
    bridgeObj["errors"] = bridgeStats_.commandErrors;
    bridgeObj["sent_text"] = bridgeStats_.sentText;
    bridgeObj["sent_binary"] = bridgeStats_.sentBinary;
    bridgeObj["sent_reliable"] = bridgeStats_.sentReliable;
    bridgeObj["rx_reliable"] = bridgeStats_.rxReliable;

    MeshStats meshStats{};
    if (mesh_ != nullptr) {
      mesh_->getStats(&meshStats);
    }
    JsonObject meshObj = out.createNestedObject("mesh");
    meshObj["tx_frames"] = meshStats.txFrames;
    meshObj["tx_success"] = meshStats.txSuccess;
    meshObj["tx_failed"] = meshStats.txFailed;
    meshObj["tx_no_mem_retries"] = meshStats.txNoMemRetries;
    meshObj["tx_no_mem_drops"] = meshStats.txNoMemDrops;
    meshObj["tx_result_queue_dropped"] = meshStats.txResultQueueDropped;
    meshObj["rx_frames"] = meshStats.rxFrames;
    meshObj["rx_queue_dropped"] = meshStats.rxQueueDropped;
    meshObj["rx_parse_errors"] = meshStats.rxParseErrors;
    meshObj["forwarded_frames"] = meshStats.forwardedFrames;
    meshObj["dropped_duplicates"] = meshStats.droppedDuplicates;
    meshObj["dropped_ttl"] = meshStats.droppedTtl;
    meshObj["reassembly_completed"] = meshStats.reassemblyCompleted;
    meshObj["reassembly_timeouts"] = meshStats.reassemblyTimeouts;
    meshObj["nodeinfo_sent"] = meshStats.nodeInfoSent;
    meshObj["nodeinfo_received"] = meshStats.nodeInfoReceived;
    meshObj["route_lookup_hit"] = meshStats.routeLookupHit;
    meshObj["route_lookup_miss"] = meshStats.routeLookupMiss;
    meshObj["route_learned"] = meshStats.routeLearned;
    meshObj["route_promoted"] = meshStats.routePromoted;
    meshObj["route_expired"] = meshStats.routeExpired;
    meshObj["routed_unicast_attempts"] = meshStats.routedUnicastAttempts;
    meshObj["routed_unicast_success"] = meshStats.routedUnicastSuccess;
    meshObj["routed_unicast_fail"] = meshStats.routedUnicastFail;
    meshObj["routed_fallback_flood"] = meshStats.routedFallbackFlood;

    BleRelayStats bleStats{};
    if (ble_ != nullptr) {
      ble_->getStats(&bleStats);
    }
    JsonObject bleObj = out.createNestedObject("ble");
    bleObj["tx_attempts"] = bleStats.txAttempts;
    bleObj["tx_rejected"] = bleStats.txRejected;
    bleObj["rx_messages"] = bleStats.rxMessages;
    bleObj["dropped_duplicates"] = bleStats.droppedDuplicates;
    bleObj["forwarded"] = bleStats.forwarded;
    bleObj["not_implemented"] = bleStats.notImplemented;

    serializeJson(out, *serial_);
    serial_->println();
    return;
  }

  if (cmd == "get_nodes") {
    DynamicJsonDocument out(2048);
    out["event"] = "nodes";
    out["type"] = "node_list";

    JsonArray nodes = out.createNestedArray("nodes");
    size_t total = 0;
    size_t exported = 0;
    bool truncated = false;
    if (mesh_ != nullptr) {
      NodeRecord records[kMaxKnownNodes];
      const size_t count = mesh_->copyNodeRecords(records, kMaxKnownNodes);
      total = count;
      for (size_t i = 0; i < count; ++i) {
        JsonObject node = nodes.createNestedObject();
        if (node.isNull()) {
          truncated = true;
          break;
        }
        node["node_id"] = formatNodeId(records[i].nodeId);
        node["last_seen_ms"] = records[i].lastSeenMs;
        node["rssi"] = records[i].lastRssi;
        node["uptime_sec"] = records[i].uptimeSec;
        node["free_heap"] = records[i].freeHeap;
        node["remote_rx_frames"] = records[i].remoteRxFrames;
        node["remote_tx_frames"] = records[i].remoteTxFrames;
        if (records[i].hasMac) {
          node["mac"] = formatMac(records[i].staMac);
        }
        exported++;
      }
    }
    out["count"] = exported;
    out["total"] = total;
    out["truncated"] = truncated;

    serializeJson(out, *serial_);
    serial_->println();
    return;
  }

  if (cmd == "get_routes") {
    DynamicJsonDocument out(4096);
    out["event"] = "routes";
    out["type"] = "route_list";

    JsonArray routes = out.createNestedArray("routes");
    if (mesh_ != nullptr) {
      RouteRecord records[kMaxRouteEntries];
      const size_t count = mesh_->copyRouteRecords(records, kMaxRouteEntries);
      const uint32_t nowMs = millis();
      size_t exported = 0;
      for (size_t i = 0; i < count; ++i) {
        JsonObject route = routes.createNestedObject();
        if (route.isNull()) {
          break;
        }
        route["dst_node_id"] = formatNodeId(records[i].dstNodeId);
        if (records[i].nextHopNodeId != 0) {
          route["next_hop_node_id"] = formatNodeId(records[i].nextHopNodeId);
        }
        if (records[i].hasNextHopMac) {
          route["next_hop_mac"] = formatMac(records[i].nextHopMac);
        }
        route["hops"] = records[i].hops;
        route["rank"] = records[i].rank;
        route["metric_q8"] = records[i].metricQ8;
        route["learned_ms"] = records[i].learnedMs;
        route["age_ms"] = nowMs - records[i].learnedMs;
        exported++;
      }
      out["count"] = exported;
      out["total"] = count;
    } else {
      out["count"] = 0;
      out["total"] = 0;
    }

    serializeJson(out, *serial_);
    serial_->println();
    return;
  }

  if (cmd == "ping") {
    StaticJsonDocument<192> out;
    out["event"] = "pong";
    out["type"] = "pong";
    out["seq"] = request["seq"] | 0;
    out["src"] = selfNodeId;
    out["latency_ms"] = 0;
    serializeJson(out, *serial_);
    serial_->println();
    return;
  }

  bridgeStats_.commandErrors++;
  emitError("unknown_cmd", cmd.c_str());
}

void SerialJsonBridge::emitError(const char* code, const char* detail) {
  if (serial_ == nullptr) {
    return;
  }
  StaticJsonDocument<256> doc;
  doc["event"] = "error";
  doc["type"] = "error";
  doc["code"] = (code != nullptr) ? code : "unknown";
  if (detail != nullptr && detail[0] != '\0') {
    doc["detail"] = detail;
  }
  serializeJson(doc, *serial_);
  serial_->println();
}

void SerialJsonBridge::emitAck(const char* cmd, bool ok, const char* via, uint32_t messageId) {
  if (serial_ == nullptr) {
    return;
  }
  StaticJsonDocument<256> doc;
  doc["event"] = "ack";
  doc["type"] = "ack";
  doc["cmd"] = (cmd != nullptr) ? cmd : "";
  doc["ok"] = ok;
  doc["via"] = (via != nullptr) ? via : "wifi";
  if (messageId != 0) {
    doc["msg_id"] = messageId;
  }
  serializeJson(doc, *serial_);
  serial_->println();
}

void SerialJsonBridge::emitMeshMessage(const ReassembledMessage& message) {
  if (serial_ == nullptr) {
    return;
  }

  const String selfNodeId = (mesh_ != nullptr) ? formatNodeId(mesh_->nodeId()) : String("0x00000000");
  const int rxRssi = static_cast<int>(message.rssi);
  const String viaMac = message.hasSenderMac ? formatMac(message.senderMac) : String("");
  uint32_t viaNodeIdRaw = 0;
  const bool hasViaNode = message.hasSenderMac && (mesh_ != nullptr) &&
                          mesh_->resolveNodeIdByMac(message.senderMac, &viaNodeIdRaw);
  const String viaNodeId = hasViaNode ? formatNodeId(viaNodeIdRaw) : String("");

  if (message.payloadType == AppPayloadType::Text) {
    String text;
    text.reserve(message.length);
    for (uint16_t i = 0; i < message.length; ++i) {
      text += static_cast<char>(message.data[i]);
    }

    DynamicJsonDocument appDoc(1400);
    const DeserializationError appErr = deserializeJson(appDoc, text);
    if (!appErr && std::strcmp(appDoc["app"] | "", "lpwa") == 0) {
      const char* appType = appDoc["type"] | "";
      const char* dst = appDoc["dst"] | "";
      const bool accepted = isSelfTarget(selfNodeId, dst);
      const auto maybeSendDeliveryAck = [&](const char* ackFor) {
        if (!accepted || mesh_ == nullptr || isBroadcastTarget(dst)) {
          return;
        }
        const bool needAck = appDoc["need_ack"] | false;
        if (!needAck) {
          return;
        }
        const char* src = appDoc["src"] | "";
        const char* e2eId = appDoc["e2e_id"] | "";
        if (src[0] == '\0' || e2eId[0] == '\0' || equalsIgnoreCase(selfNodeId, src)) {
          return;
        }

        DynamicJsonDocument ack(448);
        ack["app"] = "lpwa";
        ack["type"] = "delivery_ack";
        ack["src"] = selfNodeId;
        ack["dst"] = src;
        ack["ack_for"] = (ackFor != nullptr) ? ackFor : "";
        ack["e2e_id"] = e2eId;
        ack["msg_id"] = message.messageId;
        ack["status"] = "ok";
        ack["request_hops"] = message.hops;
        if (appDoc.containsKey("text_id")) {
          ack["text_id"] = appDoc["text_id"] | "";
        } else if (appDoc.containsKey("id")) {
          ack["text_id"] = appDoc["id"] | "";
        }
        if (appDoc.containsKey("index")) {
          ack["index"] = appDoc["index"] | 0;
        } else if (appDoc.containsKey("i")) {
          ack["index"] = appDoc["i"] | 0;
        }
        if (appDoc.containsKey("retry_no")) {
          ack["retry_no"] = appDoc["retry_no"] | 0;
        }

        String response;
        serializeJson(ack, response);
        const uint8_t ttl = appDoc["ttl"] | kDefaultTtl;
        uint32_t dstNodeId = 0;
        if (!parseNodeIdText(src, &dstNodeId)) {
          return;
        }
        mesh_->sendTextDirected(response.c_str(), dstNodeId, ttl, nullptr);
      };

      const auto emitTraceTelemetry = [&]() {
#if !LPWA_ENABLE_TRACE_TELEMETRY
        return;
#endif
        if (mesh_ == nullptr || appType[0] == '\0' || std::strcmp(appType, "trace_obs") == 0) {
          return;
        }
        if (std::strcmp(appType, "delivery_ack") == 0) {
          return;
        }
        if (std::strcmp(appType, "long_text_chunk") == 0 || std::strcmp(appType, "r1k_d") == 0 ||
            std::strcmp(appType, "r1k_r") == 0 ||
            std::strcmp(appType, "reliable_1k_chunk") == 0 || std::strcmp(appType, "reliable_1k_repair") == 0) {
          // 高頻度チャンクはトレース配信を抑制し、テレメトリ過負荷を避ける。
          return;
        }
        const uint32_t nowMs = millis();
        if ((nowMs - lastTraceTelemetryMs_) < kTraceTelemetryMinIntervalMs) {
          return;
        }
        lastTraceTelemetryMs_ = nowMs;
        DynamicJsonDocument trace(512);
        trace["app"] = "lpwa";
        trace["type"] = "trace_obs";
        trace["observer"] = selfNodeId;
        trace["app_type"] = appType;
        trace["src"] = appDoc["src"] | formatNodeId(message.originId);
        trace["dst"] = (dst[0] == '\0') ? "*" : dst;
        trace["msg_id"] = message.messageId;
        trace["hops"] = message.hops;
        if (appDoc.containsKey("request_hops")) {
          trace["request_hops"] = appDoc["request_hops"] | 0;
        }
        if (appDoc.containsKey("reply_hops")) {
          trace["reply_hops"] = appDoc["reply_hops"] | 0;
        } else if (std::strcmp(appType, "pong") == 0 || std::strcmp(appType, "delivery_ack") == 0) {
          trace["reply_hops"] = message.hops;
        }
        trace["rssi"] = rxRssi;
        if (!viaMac.isEmpty()) {
          trace["via_mac"] = viaMac;
        }
        if (!viaNodeId.isEmpty()) {
          trace["via_node"] = viaNodeId;
        }
        if (appDoc.containsKey("e2e_id")) {
          trace["e2e_id"] = appDoc["e2e_id"] | "";
        }
        if (appDoc.containsKey("retry_no")) {
          trace["retry_no"] = appDoc["retry_no"] | 0;
        }
        String wire;
        serializeJson(trace, wire);
        if (wire.length() <= kMaxAppPayload) {
          mesh_->sendText(wire.c_str(), kTraceTelemetryTtl, nullptr);
        }
      };

      const auto emitObserved = [&]() {
        if (appType[0] == '\0' || std::strcmp(appType, "trace_obs") == 0) {
          return;
        }
        DynamicJsonDocument observed(640);
        observed["event"] = "mesh_observed";
        observed["type"] = "mesh_observed";
        observed["app_type"] = appType;
        observed["via"] = "wifi";
        observed["observer"] = selfNodeId;
        observed["src"] = appDoc["src"] | formatNodeId(message.originId);
        observed["dst"] = (dst[0] == '\0') ? "*" : dst;
        observed["msg_id"] = message.messageId;
        observed["hops"] = message.hops;
        if (appDoc.containsKey("request_hops")) {
          observed["request_hops"] = appDoc["request_hops"] | 0;
        }
        if (appDoc.containsKey("reply_hops")) {
          observed["reply_hops"] = appDoc["reply_hops"] | 0;
        } else if (std::strcmp(appType, "pong") == 0 || std::strcmp(appType, "delivery_ack") == 0) {
          observed["reply_hops"] = message.hops;
        }
        observed["rssi"] = rxRssi;
        if (!viaMac.isEmpty()) {
          observed["via_mac"] = viaMac;
        }
        if (!viaNodeId.isEmpty()) {
          observed["via_node"] = viaNodeId;
        }
        if (appDoc.containsKey("e2e_id")) {
          observed["e2e_id"] = appDoc["e2e_id"] | "";
        }
        if (appDoc.containsKey("retry_no")) {
          observed["retry_no"] = appDoc["retry_no"] | 0;
        }
        if (appDoc.containsKey("text_id")) {
          observed["text_id"] = appDoc["text_id"] | "";
        } else if (appDoc.containsKey("id")) {
          observed["text_id"] = appDoc["id"] | "";
        }
        if (appDoc.containsKey("r1k_id")) {
          observed["r1k_id"] = appDoc["r1k_id"] | "";
        } else if (appDoc.containsKey("id")) {
          observed["r1k_id"] = appDoc["id"] | "";
        }
        if (appDoc.containsKey("profile_id")) {
          observed["profile_id"] = appDoc["profile_id"] | 0;
        } else if (appDoc.containsKey("pf")) {
          observed["profile_id"] = appDoc["pf"] | 0;
        }
        if (appDoc.containsKey("index")) {
          observed["index"] = appDoc["index"] | 0;
        } else if (appDoc.containsKey("i")) {
          observed["index"] = appDoc["i"] | 0;
        }
        serializeJson(observed, *serial_);
        serial_->println();
        emitTraceTelemetry();
      };

      if (std::strcmp(appType, "trace_obs") == 0) {
        DynamicJsonDocument traceOut(768);
        traceOut["event"] = "mesh_trace";
        traceOut["type"] = "mesh_trace";
        traceOut["via"] = "wifi";
        traceOut["observer"] = appDoc["observer"] | (appDoc["src"] | formatNodeId(message.originId));
        traceOut["app_type"] = appDoc["app_type"] | "";
        traceOut["src"] = appDoc["src"] | formatNodeId(message.originId);
        traceOut["dst"] = appDoc["dst"] | "*";
        traceOut["msg_id"] = appDoc["msg_id"] | 0;
        traceOut["hops"] = appDoc["hops"] | 0;
        if (appDoc.containsKey("request_hops")) {
          traceOut["request_hops"] = appDoc["request_hops"] | 0;
        }
        if (appDoc.containsKey("reply_hops")) {
          traceOut["reply_hops"] = appDoc["reply_hops"] | 0;
        }
        traceOut["rssi"] = appDoc["rssi"] | 0;
        traceOut["trace_msg_id"] = message.messageId;
        if (!viaMac.isEmpty()) {
          traceOut["relay_mac"] = viaMac;
        }
        if (appDoc.containsKey("via_mac")) {
          traceOut["via_mac"] = appDoc["via_mac"] | "";
        }
        if (appDoc.containsKey("via_node")) {
          traceOut["via_node"] = appDoc["via_node"] | "";
        }
        if (appDoc.containsKey("e2e_id")) {
          traceOut["e2e_id"] = appDoc["e2e_id"] | "";
        }
        if (appDoc.containsKey("retry_no")) {
          traceOut["retry_no"] = appDoc["retry_no"] | 0;
        }
        serializeJson(traceOut, *serial_);
        serial_->println();
        return;
      }

      emitObserved();

      if (std::strcmp(appType, "delivery_ack") == 0) {
        const char* ackDst = appDoc["dst"] | "";
        if (ackDst[0] == '\0' || isBroadcastTarget(ackDst) || !equalsIgnoreCase(selfNodeId, ackDst)) {
          return;
        }
        DynamicJsonDocument out(448);
        out["event"] = "delivery_ack";
        out["type"] = "delivery_ack";
        out["via"] = "wifi";
        out["src"] = appDoc["src"] | formatNodeId(message.originId);
        out["dst"] = appDoc["dst"] | "";
        out["ack_for"] = appDoc["ack_for"] | "";
        out["e2e_id"] = appDoc["e2e_id"] | "";
        out["msg_id"] = appDoc["msg_id"] | 0;
        out["rx_msg_id"] = message.messageId;
        out["status"] = appDoc["status"] | "ok";
        out["hops"] = message.hops;
        out["reply_hops"] = message.hops;
        if (appDoc.containsKey("request_hops")) {
          out["request_hops"] = appDoc["request_hops"] | 0;
        }
        out["rssi"] = rxRssi;
        if (!viaMac.isEmpty()) {
          out["via_mac"] = viaMac;
        }
        if (!viaNodeId.isEmpty()) {
          out["via_node"] = viaNodeId;
        }
        if (appDoc.containsKey("text_id")) {
          out["text_id"] = appDoc["text_id"] | "";
        } else if (appDoc.containsKey("id")) {
          out["text_id"] = appDoc["id"] | "";
        }
        if (appDoc.containsKey("index")) {
          out["index"] = appDoc["index"] | 0;
        } else if (appDoc.containsKey("i")) {
          out["index"] = appDoc["i"] | 0;
        }
        if (appDoc.containsKey("retry_no")) {
          out["retry_no"] = appDoc["retry_no"] | 0;
        }
        serializeJson(out, *serial_);
        serial_->println();
        return;
      }

      if (std::strcmp(appType, "ping") == 0) {
        if (accepted && mesh_ != nullptr) {
          const char* src = appDoc["src"] | "";
          if (src[0] != '\0' && !equalsIgnoreCase(selfNodeId, src)) {
            StaticJsonDocument<320> pong;
            pong["app"] = "lpwa";
            pong["type"] = "pong";
            pong["src"] = selfNodeId;
            pong["dst"] = src;
            pong["seq"] = appDoc["seq"] | 0;
            pong["ping_id"] = appDoc["ping_id"] | "";
            const uint32_t ts = appDoc["ts_ms"] | 0;
            pong["latency_ms"] = (ts == 0) ? 0 : (millis() - ts);
            pong["request_hops"] = message.hops;

            String response;
            serializeJson(pong, response);
            const uint8_t ttl = appDoc["ttl"] | kDefaultTtl;
            uint32_t dstNodeId = 0;
            if (!parseNodeIdText(src, &dstNodeId)) {
              return;
            }
            mesh_->sendTextDirected(response.c_str(), dstNodeId, ttl, nullptr);
          }
        }
        return;
      }

      if (std::strcmp(appType, "pong") == 0) {
        if (!accepted) {
          return;
        }
        DynamicJsonDocument out(448);
        out["event"] = "pong";
        out["type"] = "pong";
        out["src"] = appDoc["src"] | formatNodeId(message.originId);
        out["dst"] = appDoc["dst"] | "";
        out["seq"] = appDoc["seq"] | 0;
        out["ping_id"] = appDoc["ping_id"] | "";
        out["msg_id"] = message.messageId;
        out["latency_ms"] = appDoc["latency_ms"] | 0;
        out["reply_hops"] = message.hops;
        if (appDoc.containsKey("request_hops")) {
          out["request_hops"] = appDoc["request_hops"] | 0;
        }
        if (appDoc.containsKey("probe_bytes")) {
          out["probe_bytes"] = appDoc["probe_bytes"] | 0;
        }
        if (appDoc.containsKey("probe_hash_ok")) {
          out["probe_hash_ok"] = appDoc["probe_hash_ok"] | false;
        }
        if (appDoc.containsKey("probe_hash")) {
          out["probe_hash"] = appDoc["probe_hash"] | "";
        }
        out["hops"] = message.hops;
        out["rssi"] = rxRssi;
        if (!viaMac.isEmpty()) {
          out["via_mac"] = viaMac;
        }
        if (!viaNodeId.isEmpty()) {
          out["via_node"] = viaNodeId;
        }
        serializeJson(out, *serial_);
        serial_->println();
        return;
      }

      if (std::strcmp(appType, "chat") == 0) {
        if (!accepted) {
          return;
        }
        DynamicJsonDocument out(512);
        out["event"] = "mesh_rx";
        out["type"] = "chat";
        out["via"] = "wifi";
        out["src"] = appDoc["src"] | formatNodeId(message.originId);
        out["dst"] = appDoc["dst"] | "";
        out["text"] = appDoc["text"] | "";
        out["msg_id"] = message.messageId;
        out["hops"] = message.hops;
        out["rssi"] = rxRssi;
        if (!viaMac.isEmpty()) {
          out["via_mac"] = viaMac;
        }
        if (!viaNodeId.isEmpty()) {
          out["via_node"] = viaNodeId;
        }
        if (appDoc.containsKey("e2e_id")) {
          out["e2e_id"] = appDoc["e2e_id"] | "";
        }
        if (appDoc.containsKey("retry_no")) {
          out["retry_no"] = appDoc["retry_no"] | 0;
        }
        serializeJson(out, *serial_);
        serial_->println();
        maybeSendDeliveryAck("chat");
        return;
      }

      const bool isR1kStart =
          (std::strcmp(appType, "reliable_1k_start") == 0) || (std::strcmp(appType, "r1k_s") == 0);
      const bool isR1kChunk =
          (std::strcmp(appType, "reliable_1k_chunk") == 0) || (std::strcmp(appType, "r1k_d") == 0);
      const bool isR1kEnd = (std::strcmp(appType, "reliable_1k_end") == 0) || (std::strcmp(appType, "r1k_e") == 0);
      const bool isR1kNack =
          (std::strcmp(appType, "reliable_1k_nack") == 0) || (std::strcmp(appType, "r1k_n") == 0);
      const bool isR1kRepair =
          (std::strcmp(appType, "reliable_1k_repair") == 0) || (std::strcmp(appType, "r1k_r") == 0);
      const bool isR1kResult =
          (std::strcmp(appType, "reliable_1k_result") == 0) || (std::strcmp(appType, "r1k_o") == 0);
      if (isR1kStart || isR1kChunk || isR1kEnd || isR1kNack || isR1kRepair || isR1kResult) {
        if (!accepted) {
          return;
        }

        bool validPayload = true;
        DynamicJsonDocument out(1800);
        out["event"] = "mesh_rx";
        if (isR1kStart) {
          out["type"] = "reliable_1k_start";
        } else if (isR1kChunk) {
          out["type"] = "reliable_1k_chunk";
        } else if (isR1kEnd) {
          out["type"] = "reliable_1k_end";
        } else if (isR1kNack) {
          out["type"] = "reliable_1k_nack";
        } else if (isR1kRepair) {
          out["type"] = "reliable_1k_repair";
        } else {
          out["type"] = "reliable_1k_result";
        }
        out["via"] = "wifi";
        out["src"] = appDoc["src"] | formatNodeId(message.originId);
        out["dst"] = appDoc["dst"] | "";
        out["msg_id"] = message.messageId;
        out["hops"] = message.hops;
        out["rssi"] = rxRssi;
        if (!viaMac.isEmpty()) {
          out["via_mac"] = viaMac;
        }
        if (!viaNodeId.isEmpty()) {
          out["via_node"] = viaNodeId;
        }
        if (appDoc.containsKey("e2e_id")) {
          out["e2e_id"] = appDoc["e2e_id"] | "";
        }
        if (appDoc.containsKey("retry_no")) {
          out["retry_no"] = appDoc["retry_no"] | 0;
        }

        String r1kId = appDoc["r1k_id"] | "";
        if (r1kId.isEmpty()) {
          r1kId = appDoc["id"] | "";
        }
        if (r1kId.isEmpty()) {
          validPayload = false;
        }
        out["r1k_id"] = r1kId;

        if (isR1kStart) {
          const int profileId = appDoc.containsKey("profile_id") ? (appDoc["profile_id"] | -1) : (appDoc["pf"] | -1);
          const int dataShards = appDoc.containsKey("data_shards") ? (appDoc["data_shards"] | -1) : (appDoc["k"] | -1);
          const int parityShards =
              appDoc.containsKey("parity_shards") ? (appDoc["parity_shards"] | -1) : (appDoc["m"] | -1);
          const int shardSize = appDoc.containsKey("shard_size") ? (appDoc["shard_size"] | -1) : (appDoc["s"] | -1);
          const int size = appDoc.containsKey("size") ? (appDoc["size"] | -1) : (appDoc["z"] | -1);
          if (profileId < 0 || dataShards <= 0 || parityShards < 0 || shardSize <= 0 || size < 0) {
            validPayload = false;
          }
          out["version"] = appDoc["version"] | (appDoc["v"] | 1);
          out["profile_id"] = profileId;
          out["data_shards"] = dataShards;
          out["parity_shards"] = parityShards;
          out["shard_size"] = shardSize;
          out["size"] = size;
          String crc = appDoc["crc32"] | "";
          if (crc.isEmpty()) {
            crc = appDoc["c"] | "";
          }
          if (!crc.isEmpty()) {
            out["crc32"] = crc;
          }
          String sha = appDoc["sha256"] | "";
          if (sha.isEmpty()) {
            sha = appDoc["h"] | "";
          }
          if (!sha.isEmpty()) {
            out["sha256"] = sha;
          }
        } else if (isR1kChunk || isR1kRepair) {
          const int index = appDoc.containsKey("index") ? (appDoc["index"] | -1) : (appDoc["i"] | -1);
          String chunkData = appDoc["data_b64"] | "";
          if (chunkData.isEmpty()) {
            chunkData = appDoc["d"] | "";
          }
          if (index < 0 || chunkData.isEmpty()) {
            validPayload = false;
          }
          out["index"] = index;
          out["data_b64"] = chunkData;
        } else if (isR1kEnd) {
          const int size = appDoc.containsKey("size") ? (appDoc["size"] | -1) : (appDoc["z"] | -1);
          const int dataShards = appDoc.containsKey("data_shards") ? (appDoc["data_shards"] | -1) : (appDoc["k"] | -1);
          const int parityShards =
              appDoc.containsKey("parity_shards") ? (appDoc["parity_shards"] | -1) : (appDoc["m"] | -1);
          const int shardSize = appDoc.containsKey("shard_size") ? (appDoc["shard_size"] | -1) : (appDoc["s"] | -1);
          if (size >= 0) {
            out["size"] = size;
          }
          if (dataShards > 0) {
            out["data_shards"] = dataShards;
          }
          if (parityShards >= 0) {
            out["parity_shards"] = parityShards;
          }
          if (shardSize > 0) {
            out["shard_size"] = shardSize;
          }
          String crc = appDoc["crc32"] | "";
          if (crc.isEmpty()) {
            crc = appDoc["c"] | "";
          }
          if (!crc.isEmpty()) {
            out["crc32"] = crc;
          }
          String sha = appDoc["sha256"] | "";
          if (sha.isEmpty()) {
            sha = appDoc["h"] | "";
          }
          if (!sha.isEmpty()) {
            out["sha256"] = sha;
          }
        } else if (isR1kNack) {
          if (!appDoc.containsKey("missing")) {
            validPayload = false;
          } else {
            out["missing"] = appDoc["missing"];
          }
        } else if (isR1kResult) {
          String status = appDoc["status"] | "";
          if (status.isEmpty()) {
            validPayload = false;
          }
          out["status"] = status;
          if (appDoc.containsKey("missing")) {
            out["missing"] = appDoc["missing"];
          }
          if (appDoc.containsKey("recovered")) {
            out["recovered"] = appDoc["recovered"] | 0;
          }
          if (appDoc.containsKey("latency_ms")) {
            out["latency_ms"] = appDoc["latency_ms"] | 0;
          }
        }

        if (!validPayload) {
          return;
        }
        serializeJson(out, *serial_);
        serial_->println();
        bridgeStats_.rxReliable++;
        if (isR1kStart) {
          maybeSendDeliveryAck("reliable_1k_start");
        } else if (isR1kEnd) {
          maybeSendDeliveryAck("reliable_1k_end");
        } else if (isR1kNack) {
          maybeSendDeliveryAck("reliable_1k_nack");
        } else if (isR1kRepair) {
          maybeSendDeliveryAck("reliable_1k_repair");
        } else {
          maybeSendDeliveryAck("reliable_1k_result");
        }
        return;
      }

      const bool isLongStart =
          (std::strcmp(appType, "long_text_start") == 0) || (std::strcmp(appType, "lt_s") == 0);
      const bool isLongChunk =
          (std::strcmp(appType, "long_text_chunk") == 0) || (std::strcmp(appType, "lt_c") == 0);
      const bool isLongEnd =
          (std::strcmp(appType, "long_text_end") == 0) || (std::strcmp(appType, "lt_e") == 0);
      if (isLongStart || isLongChunk || isLongEnd) {
        if (!accepted) {
          return;
        }

        bool validLongPayload = true;
        DynamicJsonDocument out(1400);
        out["event"] = "mesh_rx";
        out["type"] = isLongStart ? "long_text_start" : (isLongChunk ? "long_text_chunk" : "long_text_end");
        out["via"] = "wifi";
        out["src"] = appDoc["src"] | formatNodeId(message.originId);
        out["dst"] = appDoc["dst"] | "";
        out["msg_id"] = message.messageId;
        out["hops"] = message.hops;
        out["rssi"] = rxRssi;
        if (!viaMac.isEmpty()) {
          out["via_mac"] = viaMac;
        }
        if (!viaNodeId.isEmpty()) {
          out["via_node"] = viaNodeId;
        }
        String textId = appDoc["text_id"] | "";
        if (textId.isEmpty()) {
          textId = appDoc["id"] | "";
        }
        if (textId.isEmpty()) {
          validLongPayload = false;
        }
        out["text_id"] = textId;
        if (appDoc.containsKey("e2e_id")) {
          out["e2e_id"] = appDoc["e2e_id"] | "";
        }
        if (appDoc.containsKey("retry_no")) {
          out["retry_no"] = appDoc["retry_no"] | 0;
        }
        if (isLongStart) {
          const bool hasSize = appDoc.containsKey("size") || appDoc.containsKey("z");
          const bool hasChunks = appDoc.containsKey("chunks") || appDoc.containsKey("n");
          if (!hasSize || !hasChunks) {
            validLongPayload = false;
          }
          String encoding = appDoc["encoding"] | "";
          if (encoding.isEmpty()) {
            encoding = appDoc["e"] | "utf-8";
          }
          const int size = hasSize ? (appDoc.containsKey("size") ? (appDoc["size"] | -1) : (appDoc["z"] | -1)) : -1;
          const int chunks =
              hasChunks ? (appDoc.containsKey("chunks") ? (appDoc["chunks"] | -1) : (appDoc["n"] | -1)) : -1;
          if (size < 0 || chunks < 0) {
            validLongPayload = false;
          }
          out["encoding"] = encoding;
          out["size"] = size;
          out["chunks"] = chunks;
          String sha = appDoc["sha256"] | "";
          if (sha.isEmpty()) {
            sha = appDoc["h"] | "";
          }
          if (!sha.isEmpty()) {
            out["sha256"] = sha;
          }
        } else if (isLongEnd) {
          if (appDoc.containsKey("encoding") || appDoc.containsKey("e")) {
            String encoding = appDoc["encoding"] | "";
            if (encoding.isEmpty()) {
              encoding = appDoc["e"] | "utf-8";
            }
            out["encoding"] = encoding;
          }
          if (appDoc.containsKey("size") || appDoc.containsKey("z")) {
            const int size = appDoc.containsKey("size") ? (appDoc["size"] | 0) : (appDoc["z"] | 0);
            out["size"] = size;
          }
          if (appDoc.containsKey("chunks") || appDoc.containsKey("n")) {
            const int chunks = appDoc.containsKey("chunks") ? (appDoc["chunks"] | 0) : (appDoc["n"] | 0);
            out["chunks"] = chunks;
          }
          String sha = appDoc["sha256"] | "";
          if (sha.isEmpty()) {
            sha = appDoc["h"] | "";
          }
          if (!sha.isEmpty()) {
            out["sha256"] = sha;
          }
        } else if (isLongChunk) {
          const int index = appDoc.containsKey("index") ? (appDoc["index"] | -1) : (appDoc["i"] | -1);
          String chunkData = appDoc["data_b64"] | "";
          if (chunkData.isEmpty()) {
            chunkData = appDoc["d"] | "";
          }
          if (index < 0 || chunkData.isEmpty()) {
            validLongPayload = false;
          }
          out["index"] = index;
          out["data_b64"] = chunkData;
        }
        if (!validLongPayload) {
          return;
        }
        serializeJson(out, *serial_);
        serial_->println();
        maybeSendDeliveryAck(isLongStart ? "long_text_start" : (isLongChunk ? "long_text_chunk" : "long_text_end"));
        return;
      }

    }

    DynamicJsonDocument out(900);
    out["event"] = "mesh_rx";
    out["type"] = "chat";
    out["via"] = "wifi";
    out["src"] = formatNodeId(message.originId);
    out["msg_id"] = message.messageId;
    out["text"] = text;
    out["hops"] = message.hops;
    out["rssi"] = rxRssi;
    if (!viaMac.isEmpty()) {
      out["via_mac"] = viaMac;
    }
    if (!viaNodeId.isEmpty()) {
      out["via_node"] = viaNodeId;
    }
    serializeJson(out, *serial_);
    serial_->println();
    return;
  }

  PingProbeHeader probe{};
  const uint8_t* probePayload = nullptr;
  if (decodePingProbePacket(message.data, message.length, &probe, &probePayload)) {
    if (probe.kind == kPingProbeKindRequest) {
      const uint32_t gotHash = fnv1a32(probePayload, probe.payloadLen);
      const bool hashOk = (gotHash == probe.payloadHash);
      if (mesh_ != nullptr && message.originId != 0 && message.originId != mesh_->nodeId()) {
        DynamicJsonDocument pong(384);
        pong["app"] = "lpwa";
        pong["type"] = "pong";
        pong["src"] = selfNodeId;
        pong["dst"] = formatNodeId(message.originId);
        pong["seq"] = probe.seq;
        pong["ping_id"] = formatHex8(probe.tag);
        pong["latency_ms"] = static_cast<uint32_t>(millis() - probe.txMs);
        pong["probe_bytes"] = probe.payloadLen;
        pong["probe_hash_ok"] = hashOk;
        pong["probe_hash"] = formatHex8(gotHash);
        pong["request_hops"] = message.hops;

        String response;
        serializeJson(pong, response);
        mesh_->sendTextDirected(response.c_str(), message.originId, kDefaultTtl, nullptr);
      }
      return;
    }

    if (probe.kind == kPingProbeKindPongOk || probe.kind == kPingProbeKindPongBad) {
      DynamicJsonDocument out(448);
      out["event"] = "pong";
      out["type"] = "pong";
      out["via"] = "wifi";
      out["src"] = formatNodeId(message.originId);
      out["dst"] = selfNodeId;
      out["seq"] = probe.seq;
      out["ping_id"] = formatHex8(probe.tag);
      out["msg_id"] = message.messageId;
      out["latency_ms"] = static_cast<uint32_t>(millis() - probe.txMs);
      out["probe_bytes"] = kPingProbeBytesDefault;
      out["probe_hash_ok"] = (probe.kind == kPingProbeKindPongOk);
      out["probe_hash"] = formatHex8(probe.payloadHash);
      out["hops"] = message.hops;
      out["reply_hops"] = message.hops;
      out["rssi"] = rxRssi;
      if (!viaMac.isEmpty()) {
        out["via_mac"] = viaMac;
      }
      if (!viaNodeId.isEmpty()) {
        out["via_node"] = viaNodeId;
      }
      serializeJson(out, *serial_);
      serial_->println();
      return;
    }
  }

  DynamicJsonDocument doc(2400);
  doc["event"] = "mesh_rx";
  doc["type"] = "binary";
  doc["via"] = "wifi";
  doc["src"] = formatNodeId(message.originId);
  doc["msg_id"] = message.messageId;
  doc["hops"] = message.hops;
  doc["rssi"] = rxRssi;
  if (!viaMac.isEmpty()) {
    doc["via_mac"] = viaMac;
  }
  if (!viaNodeId.isEmpty()) {
    doc["via_node"] = viaNodeId;
  }
  doc["data_b64"] = encodeBase64(message.data, message.length);
  serializeJson(doc, *serial_);
  serial_->println();
}

void SerialJsonBridge::emitBleMessage(const BleRelayMessage& message) {
  if (serial_ == nullptr) {
    return;
  }

  StaticJsonDocument<256> doc;
  doc["event"] = "ble_rx";
  doc["type"] = "chat";
  doc["via"] = "ble";
  doc["src"] = formatNodeId(message.originId);
  doc["msg_id"] = message.messageId;
  doc["ttl"] = message.ttl;
  doc["hops"] = message.hops;
  doc["text"] = message.text;
  serializeJson(doc, *serial_);
  serial_->println();
}

bool SerialJsonBridge::decodeBase64(const char* input, uint8_t* outBuffer, size_t outCapacity,
                                    size_t* outLen) const {
  if (input == nullptr || outBuffer == nullptr || outLen == nullptr) {
    return false;
  }
  size_t decodedLen = 0;
  const int rc = mbedtls_base64_decode(outBuffer, outCapacity, &decodedLen,
                                       reinterpret_cast<const unsigned char*>(input),
                                       std::strlen(input));
  if (rc != 0) {
    return false;
  }
  *outLen = decodedLen;
  return true;
}

String SerialJsonBridge::encodeBase64(const uint8_t* data, size_t len) const {
  if (data == nullptr || len == 0) {
    return "";
  }

  const size_t outCapacity = ((len + 2) / 3) * 4 + 1;
  std::unique_ptr<unsigned char[]> out(new unsigned char[outCapacity]);
  size_t outLen = 0;
  const int rc = mbedtls_base64_encode(out.get(), outCapacity, &outLen, data, len);
  if (rc != 0) {
    return "";
  }
  out[outLen] = '\0';
  return String(reinterpret_cast<char*>(out.get()));
}

String SerialJsonBridge::formatNodeId(uint32_t nodeId) {
  char buf[11];
  std::snprintf(buf, sizeof(buf), "0x%08" PRIX32, nodeId);
  return String(buf);
}

String SerialJsonBridge::formatMac(const uint8_t* mac) {
  if (mac == nullptr) {
    return String("");
  }
  char buf[18];
  std::snprintf(buf, sizeof(buf), "%02X:%02X:%02X:%02X:%02X:%02X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  return String(buf);
}

}  // namespace lpwa

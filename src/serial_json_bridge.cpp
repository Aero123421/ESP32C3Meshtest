#include "serial_json_bridge.h"

#include <ArduinoJson.h>
#include <mbedtls/base64.h>

#include <inttypes.h>
#include <cstring>
#include <memory>

namespace lpwa {

namespace {

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

}  // namespace

SerialJsonBridge::SerialJsonBridge(EspNowMesh* mesh, BleRelay* ble) : mesh_(mesh), ble_(ble) {}

void SerialJsonBridge::begin(Stream* stream) {
  serial_ = stream;
  lineLength_ = 0;
  bridgeStats_ = BridgeStats{};

  if (serial_ == nullptr) {
    return;
  }

  StaticJsonDocument<192> doc;
  doc["event"] = "bridge_ready";
  doc["type"] = "bridge_ready";
  if (mesh_ != nullptr) {
    doc["node_id"] = formatNodeId(mesh_->nodeId());
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
        if (lineLength_ > 0) {
          lineBuffer_[lineLength_] = '\0';
          handleLine(lineBuffer_);
          lineLength_ = 0;
        }
        continue;
      }

      if (lineLength_ >= (kLineBufferSize - 1)) {
        lineLength_ = 0;
        bridgeStats_.commandErrors++;
        emitError("line_too_long", "max 4095 bytes");
        continue;
      }
      lineBuffer_[lineLength_++] = ch;
    }
  }

  if (mesh_ != nullptr) {
    ReassembledMessage message{};
    while (mesh_->popReceivedMessage(&message)) {
      emitMeshMessage(message);
    }
  }

  if (ble_ != nullptr) {
    BleRelayMessage message{};
    while (ble_->popReceived(&message)) {
      emitBleMessage(message);
    }
  }
}

void SerialJsonBridge::handleLine(const char* line) {
  if (line == nullptr || serial_ == nullptr) {
    return;
  }

  bridgeStats_.commandCount++;

  DynamicJsonDocument request(4096);
  const DeserializationError err = deserializeJson(request, line);
  if (err) {
    bridgeStats_.commandErrors++;
    emitError("json_parse_error", err.c_str());
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
    } else if (type == "chat" || type == "ping" || type == "image_start" || type == "image_chunk" ||
               type == "image_end") {
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
      } else if (type == "image_start") {
        envelope["image_id"] = request["image_id"] | "";
        envelope["name"] = request["name"] | "";
        envelope["size"] = request["size"] | 0;
        envelope["chunks"] = request["chunks"] | 0;
        envelope["sha256"] = request["sha256"] | "";
      } else if (type == "image_chunk") {
        envelope["image_id"] = request["image_id"] | "";
        envelope["index"] = request["index"] | 0;
        envelope["data_b64"] = request["data_b64"] | "";
      } else if (type == "image_end") {
        envelope["image_id"] = request["image_id"] | "";
      }

      String wireText;
      serializeJson(envelope, wireText);
      if (wireText.length() > kMaxAppPayload) {
        bridgeStats_.commandErrors++;
        emitError("payload_too_large", "type envelope exceeds 1024 bytes");
        return;
      }

      uint32_t messageId = 0;
      const bool ok = (mesh_ != nullptr) && mesh_->sendText(wireText.c_str(), ttl, &messageId);

      if (ok) {
        if (type == "chat" || type == "ping" || type == "image_start" || type == "image_chunk" ||
            type == "image_end") {
          bridgeStats_.sentText++;
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

    uint32_t messageId = 0;
    bool ok = false;
    if (via == "wifi") {
      ok = (mesh_ != nullptr) && mesh_->sendText(text, ttl, &messageId);
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
    const bool ok = (mesh_ != nullptr) && mesh_->sendBinary(decoded, decodedLen, ttl, &messageId);
    if (ok) {
      bridgeStats_.sentBinary++;
    }
    emitAck("send_binary", ok, "wifi", messageId);
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

    MeshStats meshStats{};
    if (mesh_ != nullptr) {
      mesh_->getStats(&meshStats);
    }
    JsonObject meshObj = out.createNestedObject("mesh");
    meshObj["tx_frames"] = meshStats.txFrames;
    meshObj["tx_success"] = meshStats.txSuccess;
    meshObj["tx_failed"] = meshStats.txFailed;
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
    DynamicJsonDocument out(4600);
    out["event"] = "nodes";
    out["type"] = "node_list";

    JsonArray nodes = out.createNestedArray("nodes");
    if (mesh_ != nullptr) {
      NodeRecord records[kMaxKnownNodes];
      const size_t count = mesh_->copyNodeRecords(records, kMaxKnownNodes);
      for (size_t i = 0; i < count; ++i) {
        JsonObject node = nodes.createNestedObject();
        node["node_id"] = formatNodeId(records[i].nodeId);
        node["last_seen_ms"] = records[i].lastSeenMs;
        node["rssi"] = records[i].lastRssi;
        node["uptime_sec"] = records[i].uptimeSec;
        node["free_heap"] = records[i].freeHeap;
        node["remote_rx_frames"] = records[i].remoteRxFrames;
        node["remote_tx_frames"] = records[i].remoteTxFrames;
      }
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

        StaticJsonDocument<384> ack;
        ack["app"] = "lpwa";
        ack["type"] = "delivery_ack";
        ack["src"] = selfNodeId;
        ack["dst"] = src;
        ack["ack_for"] = (ackFor != nullptr) ? ackFor : "";
        ack["e2e_id"] = e2eId;
        ack["msg_id"] = message.messageId;
        ack["status"] = "ok";
        if (appDoc.containsKey("image_id")) {
          ack["image_id"] = appDoc["image_id"] | "";
        }
        if (appDoc.containsKey("index")) {
          ack["index"] = appDoc["index"] | 0;
        }
        if (appDoc.containsKey("retry_no")) {
          ack["retry_no"] = appDoc["retry_no"] | 0;
        }

        String response;
        serializeJson(ack, response);
        mesh_->sendText(response.c_str(), appDoc["ttl"] | kDefaultTtl, nullptr);
      };

      if (std::strcmp(appType, "delivery_ack") == 0) {
        if (!accepted) {
          return;
        }
        DynamicJsonDocument out(384);
        out["event"] = "delivery_ack";
        out["type"] = "delivery_ack";
        out["via"] = "wifi";
        out["src"] = appDoc["src"] | formatNodeId(message.originId);
        out["ack_for"] = appDoc["ack_for"] | "";
        out["e2e_id"] = appDoc["e2e_id"] | "";
        out["msg_id"] = appDoc["msg_id"] | 0;
        out["status"] = appDoc["status"] | "ok";
        out["hops"] = message.hops;
        if (appDoc.containsKey("image_id")) {
          out["image_id"] = appDoc["image_id"] | "";
        }
        if (appDoc.containsKey("index")) {
          out["index"] = appDoc["index"] | 0;
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
            StaticJsonDocument<256> pong;
            pong["app"] = "lpwa";
            pong["type"] = "pong";
            pong["src"] = selfNodeId;
            pong["dst"] = src;
            pong["seq"] = appDoc["seq"] | 0;
            pong["ping_id"] = appDoc["ping_id"] | "";
            const uint32_t ts = appDoc["ts_ms"] | 0;
            pong["latency_ms"] = (ts == 0) ? 0 : (millis() - ts);

            String response;
            serializeJson(pong, response);
            mesh_->sendText(response.c_str(), appDoc["ttl"] | kDefaultTtl, nullptr);
          }
        }
        return;
      }

      if (std::strcmp(appType, "pong") == 0) {
        if (!accepted) {
          return;
        }
        StaticJsonDocument<256> out;
        out["event"] = "pong";
        out["type"] = "pong";
        out["src"] = appDoc["src"] | formatNodeId(message.originId);
        out["seq"] = appDoc["seq"] | 0;
        out["ping_id"] = appDoc["ping_id"] | "";
        out["latency_ms"] = appDoc["latency_ms"] | 0;
        out["hops"] = message.hops;
        serializeJson(out, *serial_);
        serial_->println();
        return;
      }

      if (std::strcmp(appType, "chat") == 0) {
        if (!accepted) {
          return;
        }
        StaticJsonDocument<512> out;
        out["event"] = "mesh_rx";
        out["type"] = "chat";
        out["via"] = "wifi";
        out["src"] = appDoc["src"] | formatNodeId(message.originId);
        out["text"] = appDoc["text"] | "";
        out["hops"] = message.hops;
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

      if (std::strcmp(appType, "image_start") == 0 || std::strcmp(appType, "image_chunk") == 0 ||
          std::strcmp(appType, "image_end") == 0) {
        if (!accepted) {
          return;
        }

        DynamicJsonDocument out(1400);
        out["event"] = "mesh_rx";
        out["type"] = appType;
        out["via"] = "wifi";
        out["src"] = appDoc["src"] | formatNodeId(message.originId);
        out["hops"] = message.hops;
        out["image_id"] = appDoc["image_id"] | "";
        if (appDoc.containsKey("e2e_id")) {
          out["e2e_id"] = appDoc["e2e_id"] | "";
        }
        if (appDoc.containsKey("retry_no")) {
          out["retry_no"] = appDoc["retry_no"] | 0;
        }
        if (std::strcmp(appType, "image_start") == 0) {
          out["name"] = appDoc["name"] | "";
          out["size"] = appDoc["size"] | 0;
          out["chunks"] = appDoc["chunks"] | 0;
          out["sha256"] = appDoc["sha256"] | "";
        } else if (std::strcmp(appType, "image_chunk") == 0) {
          out["index"] = appDoc["index"] | 0;
          out["data_b64"] = appDoc["data_b64"] | "";
        }
        serializeJson(out, *serial_);
        serial_->println();
        maybeSendDeliveryAck(appType);
        return;
      }
    }

    StaticJsonDocument<900> out;
    out["event"] = "mesh_rx";
    out["type"] = "chat";
    out["via"] = "wifi";
    out["src"] = formatNodeId(message.originId);
    out["text"] = text;
    out["hops"] = message.hops;
    serializeJson(out, *serial_);
    serial_->println();
    return;
  }

  DynamicJsonDocument doc(2400);
  doc["event"] = "mesh_rx";
  doc["type"] = "binary";
  doc["via"] = "wifi";
  doc["src"] = formatNodeId(message.originId);
  doc["msg_id"] = message.messageId;
  doc["hops"] = message.hops;
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

}  // namespace lpwa

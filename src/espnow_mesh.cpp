#include "espnow_mesh.h"

#include <Arduino.h>
#include <WiFi.h>
#include <esp_system.h>
#include <esp_wifi.h>

#include <cstdlib>
#include <cstring>

namespace lpwa {

namespace {
constexpr uint8_t kBroadcastMac[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
constexpr uint32_t kParseRejectDuplicateWindowMs = 250;

uint16_t randomDelayMs(uint16_t minMs, uint16_t maxMs) {
  if (maxMs < minMs) {
    return minMs;
  }
  if (maxMs == minMs) {
    return minMs;
  }
  const uint32_t span = static_cast<uint32_t>(maxMs - minMs) + 1U;
  const uint32_t r = esp_random() % span;
  return static_cast<uint16_t>(static_cast<uint32_t>(minMs) + r);
}

void delayRandomRange(uint16_t minMs, uint16_t maxMs) {
  const uint16_t waitMs = randomDelayMs(minMs, maxMs);
  if (waitMs > 0) {
    delay(waitMs);
  }
}
}

EspNowMesh* EspNowMesh::instance_ = nullptr;

EspNowMesh::EspNowMesh() {
  std::memset(nodes_, 0, sizeof(nodes_));
  std::memset(neighbors_, 0, sizeof(neighbors_));
  std::memset(routes_, 0, sizeof(routes_));
  std::memset(inboundQueue_, 0, sizeof(inboundQueue_));
}

bool EspNowMesh::begin() {
  if (instance_ != nullptr && instance_ != this) {
    return false;
  }
  instance_ = this;

  const uint64_t efuseMac = ESP.getEfuseMac();
  nodeId_ = static_cast<uint32_t>((efuseMac >> 24) ^ (efuseMac & 0x00FFFFFFULL));
  if (nodeId_ == 0) {
    nodeId_ = 1;
  }

  nextMessageId_ = esp_random();
  if (nextMessageId_ == 0) {
    nextMessageId_ = 1;
  }
  stats_ = MeshStats{};
  duplicateFilter_.clear();
  parseRejectFilter_.clear();
  nodeCount_ = 0;
  std::memset(neighbors_, 0, sizeof(neighbors_));
  std::memset(routes_, 0, sizeof(routes_));
  inboundHead_ = 0;
  inboundTail_ = 0;
  inboundCount_ = 0;

  if (rxQueue_ == nullptr) {
    rxQueue_ = xQueueCreate(kRxQueueDepth, sizeof(RxQueueItem));
  } else {
    xQueueReset(rxQueue_);
  }
  if (rxQueue_ == nullptr) {
    return false;
  }

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(20);

  // Best-effort tuning: if a platform build does not support one of these knobs,
  // continue with defaults instead of failing mesh startup.
#if LPWA_ENABLE_BLE_RELAY
  // Wi-Fi + BLE coexist requires modem sleep enabled on ESP32-C3.
  (void)esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
#else
  (void)esp_wifi_set_ps(WIFI_PS_NONE);
#endif
  (void)esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20);

  uint8_t protocolMask = WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N;
#if LPWA_ENABLE_WIFI_LR && (!LPWA_ENABLE_BLE_RELAY || LPWA_ALLOW_WIFI_LR_WITH_BLE)
#ifdef WIFI_PROTOCOL_LR
  protocolMask = static_cast<uint8_t>(protocolMask | WIFI_PROTOCOL_LR);
#endif
#endif
  esp_err_t protocolResult = esp_wifi_set_protocol(WIFI_IF_STA, protocolMask);
#if LPWA_ENABLE_WIFI_LR && (!LPWA_ENABLE_BLE_RELAY || LPWA_ALLOW_WIFI_LR_WITH_BLE)
#ifdef WIFI_PROTOCOL_LR
  if (protocolResult != ESP_OK) {
    protocolResult = esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N);
  }
#endif
#endif
  (void)protocolResult;

  hasStaMac_ = false;
  std::memset(staMac_, 0, sizeof(staMac_));
  if (esp_wifi_get_mac(WIFI_IF_STA, staMac_) == ESP_OK) {
    hasStaMac_ = true;
  }

  const esp_err_t channelResult = esp_wifi_set_channel(kMeshChannel, WIFI_SECOND_CHAN_NONE);
  if (channelResult != ESP_OK) {
    return false;
  }

  // 84 == 21.0 dBm in 0.25 dBm steps (chip/regulatory limits still apply).
  const esp_err_t txPowerResult = esp_wifi_set_max_tx_power(kMeshTxPowerQuarterDbm);
  if (txPowerResult != ESP_OK) {
    return false;
  }

  const esp_err_t initResult = esp_now_init();
  if (initResult != ESP_OK && initResult != ESP_ERR_ESPNOW_EXIST) {
    return false;
  }

  esp_now_register_send_cb(EspNowMesh::onSendStatic);
  esp_now_register_recv_cb(EspNowMesh::onRecvStatic);

  esp_now_peer_info_t peerInfo{};
  std::memcpy(peerInfo.peer_addr, kBroadcastMac, sizeof(kBroadcastMac));
  peerInfo.channel = kMeshChannel;
  peerInfo.encrypt = false;
  peerInfo.ifidx = WIFI_IF_STA;

  const esp_err_t peerResult = esp_now_add_peer(&peerInfo);
  if (peerResult != ESP_OK && peerResult != ESP_ERR_ESPNOW_EXIST) {
    return false;
  }

  NodeRecord* selfNode = nullptr;
  upsertNode(nodeId_, &selfNode);
  if (selfNode != nullptr) {
    selfNode->lastSeenMs = millis();
    selfNode->lastRssi = 0;
    selfNode->hasMac = hasStaMac_;
    if (hasStaMac_) {
      std::memcpy(selfNode->staMac, staMac_, sizeof(staMac_));
    }
    selfNode->uptimeSec = 0;
    selfNode->freeHeap = ESP.getFreeHeap();
  }

  const uint32_t nowMs = millis();
  const uint32_t firstDelay = randomDelayMs(kNodeInfoInitialJitterMinMs, kNodeInfoInitialJitterMaxMs);
  nextNodeInfoDueMs_ = nowMs + firstDelay;
  return true;
}

void EspNowMesh::loop() {
  processRxQueue();

  uint32_t droppedByTimeout = 0;
  reassembly_.PruneExpired(millis(), kReassemblyTimeoutMs, &droppedByTimeout);
  stats_.reassemblyTimeouts += droppedByTimeout;

  const uint32_t nowMs = millis();
  if (static_cast<int32_t>(nowMs - nextNodeInfoDueMs_) >= 0) {
    sendNodeInfo(3);
    const uint32_t jitterMs = randomDelayMs(0, kNodeInfoJitterMaxMs);
    nextNodeInfoDueMs_ = nowMs + kNodeInfoPeriodMs + jitterMs;
  }
}

bool EspNowMesh::sendText(const char* text, uint8_t ttl, uint32_t* outMessageId) {
  if (text == nullptr) {
    return false;
  }
  const size_t len = std::strlen(text);
  return sendPayload(AppPayloadType::Text, reinterpret_cast<const uint8_t*>(text), len, ttl,
                     outMessageId);
}

bool EspNowMesh::sendBinary(const uint8_t* payload, size_t len, uint8_t ttl, uint32_t* outMessageId) {
  if (len > 0 && payload == nullptr) {
    return false;
  }
  return sendPayload(AppPayloadType::Binary, payload, len, ttl, outMessageId);
}

bool EspNowMesh::sendTextDirected(const char* text, uint32_t dstNodeId, uint8_t ttl, uint32_t* outMessageId) {
  if (text == nullptr) {
    return false;
  }
  const size_t len = std::strlen(text);
  return sendPayloadDirected(AppPayloadType::Text, reinterpret_cast<const uint8_t*>(text), len, dstNodeId,
                             ttl, outMessageId);
}

bool EspNowMesh::sendBinaryDirected(const uint8_t* payload, size_t len, uint32_t dstNodeId, uint8_t ttl,
                                    uint32_t* outMessageId) {
  if (len > 0 && payload == nullptr) {
    return false;
  }
  return sendPayloadDirected(AppPayloadType::Binary, payload, len, dstNodeId, ttl, outMessageId);
}

bool EspNowMesh::popReceivedMessage(ReassembledMessage* outMessage) {
  if (outMessage == nullptr || inboundCount_ == 0) {
    return false;
  }

  *outMessage = inboundQueue_[inboundHead_];
  inboundHead_ = (inboundHead_ + 1) % kInboundMessageQueueDepth;
  inboundCount_--;
  return true;
}

void EspNowMesh::getStats(MeshStats* outStats) const {
  if (outStats == nullptr) {
    return;
  }
  *outStats = stats_;
}

size_t EspNowMesh::copyNodeRecords(NodeRecord* outRecords, size_t maxRecords) const {
  if (outRecords == nullptr || maxRecords == 0) {
    return 0;
  }
  const size_t count = (nodeCount_ < maxRecords) ? nodeCount_ : maxRecords;
  for (size_t i = 0; i < count; ++i) {
    outRecords[i] = nodes_[i];
  }
  return count;
}

size_t EspNowMesh::copyRouteRecords(RouteRecord* outRecords, size_t maxRecords) const {
  if (outRecords == nullptr || maxRecords == 0) {
    return 0;
  }
  size_t count = 0;
  const uint32_t nowMs = millis();
  for (size_t i = 0; i < kMaxRouteEntries && count < maxRecords; ++i) {
    const RouteEntry& route = routes_[i];
    if (!route.used || route.dstNodeId == 0) {
      continue;
    }
    const uint8_t zeroMac[6] = {0, 0, 0, 0, 0, 0};
    if (std::memcmp(route.nextHopMac, zeroMac, 6) == 0) {
      continue;
    }
    if ((nowMs - route.learnedMs) > kRouteExpireMs) {
      continue;
    }
    RouteRecord rec{};
    rec.dstNodeId = route.dstNodeId;
    rec.nextHopNodeId = route.nextHopNodeId;
    rec.learnedMs = route.learnedMs;
    rec.hops = route.hops;
    rec.metricQ8 = route.metricQ8;
    rec.hasNextHopMac = true;
    std::memcpy(rec.nextHopMac, route.nextHopMac, sizeof(rec.nextHopMac));
    outRecords[count++] = rec;
  }
  return count;
}

bool EspNowMesh::resolveNodeIdByMac(const uint8_t* mac, uint32_t* outNodeId) const {
  if (outNodeId != nullptr) {
    *outNodeId = 0;
  }
  if (mac == nullptr) {
    return false;
  }
  for (size_t i = 0; i < nodeCount_; ++i) {
    const NodeRecord& node = nodes_[i];
    if (!node.hasMac) {
      continue;
    }
    if (std::memcmp(node.staMac, mac, 6) == 0) {
      if (outNodeId != nullptr) {
        *outNodeId = node.nodeId;
      }
      return true;
    }
  }
  return false;
}

void EspNowMesh::onSendStatic(const uint8_t* mac_addr, esp_now_send_status_t status) {
  if (instance_ != nullptr) {
    instance_->onSend(mac_addr, status);
  }
}

#if ESP_ARDUINO_VERSION_MAJOR >= 3
void EspNowMesh::onRecvStatic(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
  if (instance_ == nullptr) {
    return;
  }

  const uint8_t* src = nullptr;
  int8_t rssi = 0;
  if (info != nullptr) {
    src = info->src_addr;
    if (info->rx_ctrl != nullptr) {
      rssi = info->rx_ctrl->rssi;
    }
  }
  instance_->onRecv(src, rssi, data, static_cast<size_t>(len));
}
#else
void EspNowMesh::onRecvStatic(const uint8_t* mac_addr, const uint8_t* data, int len) {
  if (instance_ == nullptr) {
    return;
  }
  instance_->onRecv(mac_addr, 0, data, static_cast<size_t>(len));
}
#endif

void EspNowMesh::onSend(const uint8_t* mac_addr, esp_now_send_status_t status) {
  if (status == ESP_NOW_SEND_SUCCESS) {
    stats_.txSuccess++;
  } else {
    stats_.txFailed++;
  }
  if (mac_addr == nullptr || std::memcmp(mac_addr, kBroadcastMac, 6) == 0) {
    return;
  }
  NeighborEntry* neighbor = upsertNeighbor(mac_addr);
  if (neighbor == nullptr) {
    return;
  }
  if (status == ESP_NOW_SEND_SUCCESS) {
    if (neighbor->txOk < 0xFFFF) {
      neighbor->txOk++;
    }
  } else {
    if (neighbor->txFail < 0xFFFF) {
      neighbor->txFail++;
    }
  }
  const uint32_t total = static_cast<uint32_t>(neighbor->txOk) + static_cast<uint32_t>(neighbor->txFail);
  if (total > 0) {
    const uint16_t instantEtxQ8 = static_cast<uint16_t>((static_cast<uint32_t>(neighbor->txFail) * 256U) / total);
    const int32_t blended = static_cast<int32_t>(neighbor->etxQ8) * 7 + static_cast<int32_t>(instantEtxQ8);
    neighbor->etxQ8 = static_cast<uint16_t>((blended + 4) / 8);
  }
}

void EspNowMesh::onRecv(const uint8_t* mac_addr, int8_t rssi, const uint8_t* data, size_t len) {
  if (mac_addr != nullptr) {
    (void)ensurePeerForMac(mac_addr);
    NeighborEntry* neighbor = upsertNeighbor(mac_addr);
    if (neighbor != nullptr) {
      neighbor->lastSeenMs = millis();
      const int16_t sampleQ8 = static_cast<int16_t>(static_cast<int16_t>(rssi) << 8);
      if (neighbor->rssiEwmaQ8 == 0) {
        neighbor->rssiEwmaQ8 = sampleQ8;
      } else {
        neighbor->rssiEwmaQ8 =
            static_cast<int16_t>((static_cast<int32_t>(neighbor->rssiEwmaQ8) * 7 + sampleQ8 + 4) / 8);
      }
    }
  }
  if (!enqueueRx(mac_addr, rssi, data, len)) {
    stats_.rxQueueDropped++;
  }
}

bool EspNowMesh::enqueueRx(const uint8_t* mac_addr, int8_t rssi, const uint8_t* data, size_t len) {
  if (rxQueue_ == nullptr || data == nullptr || len == 0 || len > kEspNowMaxPayload) {
    return false;
  }

  RxQueueItem item{};
  if (mac_addr != nullptr) {
    std::memcpy(item.senderMac, mac_addr, sizeof(item.senderMac));
  } else {
    std::memset(item.senderMac, 0, sizeof(item.senderMac));
  }
  item.rssi = rssi;
  item.len = static_cast<uint16_t>(len);
  std::memcpy(item.data, data, len);

  return xQueueSend(rxQueue_, &item, 0) == pdTRUE;
}

void EspNowMesh::processRxQueue() {
  if (rxQueue_ == nullptr) {
    return;
  }

  RxQueueItem item{};
  uint8_t processed = 0;
  while (processed < kRxProcessBudgetPerLoop && xQueueReceive(rxQueue_, &item, 0) == pdTRUE) {
    processFrame(item);
    processed++;
  }
}

void EspNowMesh::processFrame(const RxQueueItem& item) {
  stats_.rxFrames++;

  MeshFrameHeader header{};
  const uint8_t* body = nullptr;
  size_t bodyLen = 0;
  if (!parseHeader(item.data, item.len, &header, &body, &bodyLen)) {
    stats_.rxParseErrors++;
    return;
  }

  const uint32_t nowMs = millis();
  pruneRoutingTables(nowMs);

  DuplicateKey dedupKey{};
  dedupKey.originId = header.originId;
  dedupKey.messageId = header.messageId;
  dedupKey.frameType = header.type;
  dedupKey.fragmentIndex = 0xFF;
  if (header.type == static_cast<uint8_t>(FrameType::Fragment)) {
    if (bodyLen < sizeof(FragmentMeta)) {
      stats_.rxParseErrors++;
      return;
    }
    FragmentMeta fragmentMeta{};
    std::memcpy(&fragmentMeta, body, sizeof(fragmentMeta));
    dedupKey.fragmentIndex = fragmentMeta.fragIndex;
  } else if (header.type == static_cast<uint8_t>(FrameType::RoutedFragment)) {
    if (bodyLen < (sizeof(RoutedFragmentMeta) + sizeof(FragmentMeta))) {
      stats_.rxParseErrors++;
      return;
    }
    FragmentMeta fragmentMeta{};
    std::memcpy(&fragmentMeta, body + sizeof(RoutedFragmentMeta), sizeof(fragmentMeta));
    dedupKey.fragmentIndex = fragmentMeta.fragIndex;
  }

  if (duplicateFilter_.seen(dedupKey, nowMs, kDuplicateWindowMs)) {
    stats_.droppedDuplicates++;
    return;
  }
  if (parseRejectFilter_.seen(dedupKey, nowMs, kParseRejectDuplicateWindowMs)) {
    stats_.droppedDuplicates++;
    return;
  }

  NodeRecord* originNode = nullptr;
  if (upsertNode(header.originId, &originNode) && originNode != nullptr) {
    originNode->lastSeenMs = nowMs;
    originNode->lastRssi = item.rssi;
  }

  bool parsedAndAccepted = false;
  bool routedFrame = false;
  RoutedFragmentMeta routedMeta{};
  if (header.type == static_cast<uint8_t>(FrameType::Fragment)) {
    parsedAndAccepted = handleFragmentFrame(header, body, bodyLen, item.rssi, item.senderMac, nowMs);
  } else if (header.type == static_cast<uint8_t>(FrameType::RoutedFragment)) {
    routedFrame = true;
    parsedAndAccepted =
        handleRoutedFragmentFrame(header, body, bodyLen, item.rssi, item.senderMac, nowMs, &routedMeta);
  } else if (header.type == static_cast<uint8_t>(FrameType::NodeInfo)) {
    parsedAndAccepted = handleNodeInfoFrame(header, body, bodyLen, item.rssi, nowMs);
  } else {
    stats_.rxParseErrors++;
    return;
  }

  if (!parsedAndAccepted) {
    parseRejectFilter_.remember(dedupKey, nowMs);
    return;
  }
  duplicateFilter_.remember(dedupKey, nowMs);
  learnRouteFromFrame(header.originId, item.senderMac, header.hops, item.rssi, nowMs);

  const bool terminalRoutedToSelf = routedFrame && routedMeta.dstNodeId == nodeId_;
  if (header.ttl <= 1) {
    if (!terminalRoutedToSelf) {
      stats_.droppedTtl++;
    }
    return;
  }

  if (terminalRoutedToSelf) {
    return;
  }

  if (item.len > kEspNowMaxPayload) {
    return;
  }

  uint8_t forwardBuffer[kEspNowMaxPayload];
  std::memcpy(forwardBuffer, item.data, item.len);
  MeshFrameHeader* forwardHeader = reinterpret_cast<MeshFrameHeader*>(forwardBuffer);
  forwardHeader->ttl = static_cast<uint8_t>(forwardHeader->ttl - 1);
  if (forwardHeader->hops < 0xFF) {
    forwardHeader->hops = static_cast<uint8_t>(forwardHeader->hops + 1);
  }

  uint8_t attempts = 1;
  if (header.type == static_cast<uint8_t>(FrameType::NodeInfo)) {
    attempts = kForwardSendAttemptsNodeInfo;
  } else {
    attempts = kForwardSendAttemptsFragment;
  }
  if (attempts == 0) {
    attempts = 1;
  }

  bool forwarded = false;
  if (routedFrame && routedMeta.dstNodeId != 0 && routedMeta.dstNodeId != nodeId_ && LPWA_ROUTING_MODE >= 2) {
    RouteEntry route{};
    if (selectRoute(routedMeta.dstNodeId, &route)) {
      stats_.routeLookupHit++;
      for (uint8_t attempt = 0; attempt < attempts; ++attempt) {
        stats_.routedUnicastAttempts++;
        if (sendRawUnicast(route.nextHopMac, forwardBuffer, item.len)) {
          stats_.routedUnicastSuccess++;
          forwarded = true;
          break;
        }
        stats_.routedUnicastFail++;
        if ((attempt + 1) < attempts) {
          delayRandomRange(kForwardJitterMinMs, kForwardJitterMaxMs);
        }
      }
    } else {
      stats_.routeLookupMiss++;
    }
    if (!forwarded) {
      stats_.routedFallbackFlood++;
    }
  }

  if (!forwarded && !(routedFrame && routedMeta.dstNodeId == nodeId_)) {
    for (uint8_t attempt = 0; attempt < attempts; ++attempt) {
      if (sendRawBroadcast(forwardBuffer, item.len)) {
        forwarded = true;
        break;
      }
      if ((attempt + 1) < attempts) {
        delayRandomRange(kForwardJitterMinMs, kForwardJitterMaxMs);
      }
    }
  }

  if (forwarded) {
    stats_.forwardedFrames++;
  }
}

bool EspNowMesh::parseHeader(const uint8_t* data, size_t len, MeshFrameHeader* outHeader,
                             const uint8_t** outBody, size_t* outBodyLen) const {
  if (data == nullptr || outHeader == nullptr || outBody == nullptr || outBodyLen == nullptr) {
    return false;
  }
  if (len < sizeof(MeshFrameHeader)) {
    return false;
  }

  std::memcpy(outHeader, data, sizeof(MeshFrameHeader));
  if (outHeader->magic != kMeshMagic || outHeader->version != kMeshVersion) {
    return false;
  }
  if (outHeader->type != static_cast<uint8_t>(FrameType::Fragment) &&
      outHeader->type != static_cast<uint8_t>(FrameType::NodeInfo) &&
      outHeader->type != static_cast<uint8_t>(FrameType::RoutedFragment)) {
    return false;
  }

  *outBody = data + sizeof(MeshFrameHeader);
  *outBodyLen = len - sizeof(MeshFrameHeader);
  return true;
}

bool EspNowMesh::handleFragmentFrame(const MeshFrameHeader& header, const uint8_t* body, size_t bodyLen,
                                     int8_t rssi, const uint8_t* senderMac, uint32_t nowMs) {
  if (body == nullptr || bodyLen < sizeof(FragmentMeta)) {
    stats_.rxParseErrors++;
    return false;
  }

  FragmentMeta fragmentMeta{};
  std::memcpy(&fragmentMeta, body, sizeof(FragmentMeta));
  const uint8_t* chunk = body + sizeof(FragmentMeta);
  const size_t chunkLen = bodyLen - sizeof(FragmentMeta);

  if (fragmentMeta.appType != static_cast<uint8_t>(AppPayloadType::Text) &&
      fragmentMeta.appType != static_cast<uint8_t>(AppPayloadType::Binary)) {
    stats_.rxParseErrors++;
    return false;
  }
  if (chunkLen != fragmentMeta.chunkLen) {
    stats_.rxParseErrors++;
    return false;
  }

  ReassembledMessage completed{};
  const bool done = reassembly_.PushFragment(
      header.originId, header.messageId, static_cast<AppPayloadType>(fragmentMeta.appType), header.hops,
      fragmentMeta.fragIndex, fragmentMeta.fragCount, fragmentMeta.totalLen, chunk, fragmentMeta.chunkLen,
      rssi, senderMac, nowMs, &completed);
  if (!done) {
    return true;
  }

  if (queueInbound(completed)) {
    stats_.reassemblyCompleted++;
  } else {
    stats_.rxQueueDropped++;
  }
  return true;
}

bool EspNowMesh::handleRoutedFragmentFrame(const MeshFrameHeader& header, const uint8_t* body, size_t bodyLen,
                                           int8_t rssi, const uint8_t* senderMac, uint32_t nowMs,
                                           RoutedFragmentMeta* outRouteMeta) {
  if (body == nullptr || bodyLen < (sizeof(RoutedFragmentMeta) + sizeof(FragmentMeta))) {
    stats_.rxParseErrors++;
    return false;
  }

  RoutedFragmentMeta routeMeta{};
  std::memcpy(&routeMeta, body, sizeof(routeMeta));
  if (outRouteMeta != nullptr) {
    *outRouteMeta = routeMeta;
  }

  FragmentMeta fragmentMeta{};
  std::memcpy(&fragmentMeta, body + sizeof(RoutedFragmentMeta), sizeof(fragmentMeta));
  if (fragmentMeta.fragCount == 0 || fragmentMeta.fragIndex >= fragmentMeta.fragCount) {
    stats_.rxParseErrors++;
    return false;
  }
  const size_t expectedBodyLen =
      sizeof(RoutedFragmentMeta) + sizeof(FragmentMeta) + static_cast<size_t>(fragmentMeta.chunkLen);
  if (expectedBodyLen != bodyLen) {
    stats_.rxParseErrors++;
    return false;
  }
  if (fragmentMeta.totalLen > kMaxAppPayload) {
    stats_.rxParseErrors++;
    return false;
  }

  if (routeMeta.dstNodeId != 0 && routeMeta.dstNodeId != nodeId_) {
    // relay-only path
    return true;
  }
  return handleFragmentFrame(header, body + sizeof(RoutedFragmentMeta),
                             bodyLen - sizeof(RoutedFragmentMeta), rssi, senderMac, nowMs);
}

bool EspNowMesh::handleNodeInfoFrame(const MeshFrameHeader& header, const uint8_t* body, size_t bodyLen,
                                     int8_t rssi, uint32_t nowMs) {
  constexpr size_t kLegacyNodeInfoPayloadSize = 22;
  if (body == nullptr || bodyLen < kLegacyNodeInfoPayloadSize) {
    stats_.rxParseErrors++;
    return false;
  }

  NodeInfoPayload remote{};
  if (bodyLen >= sizeof(remote)) {
    std::memcpy(&remote, body, sizeof(remote));
  } else {
    std::memcpy(&remote.nodeId, body + 0, sizeof(remote.nodeId));
    std::memcpy(&remote.uptimeSec, body + 4, sizeof(remote.uptimeSec));
    std::memcpy(&remote.freeHeap, body + 8, sizeof(remote.freeHeap));
    std::memcpy(&remote.rxFrames, body + 12, sizeof(remote.rxFrames));
    std::memcpy(&remote.txFrames, body + 16, sizeof(remote.txFrames));
    std::memcpy(&remote.seenNodes, body + 20, sizeof(remote.seenNodes));
    std::memset(remote.staMac, 0, sizeof(remote.staMac));
  }
  if (remote.nodeId == 0 || remote.nodeId != header.originId) {
    stats_.rxParseErrors++;
    return false;
  }

  NodeRecord* node = nullptr;
  if (!upsertNode(remote.nodeId, &node) || node == nullptr) {
    return false;
  }

  node->lastSeenMs = nowMs;
  node->lastRssi = rssi;
  node->hasMac = false;
  for (size_t i = 0; i < sizeof(remote.staMac); ++i) {
    if (remote.staMac[i] != 0) {
      node->hasMac = true;
      break;
    }
  }
  if (node->hasMac) {
    std::memcpy(node->staMac, remote.staMac, sizeof(node->staMac));
  } else {
    std::memset(node->staMac, 0, sizeof(node->staMac));
  }
  node->uptimeSec = remote.uptimeSec;
  node->freeHeap = remote.freeHeap;
  node->remoteRxFrames = remote.rxFrames;
  node->remoteTxFrames = remote.txFrames;
  stats_.nodeInfoReceived++;
  return true;
}

bool EspNowMesh::sendPayload(AppPayloadType payloadType, const uint8_t* payload, size_t len, uint8_t ttl,
                             uint32_t* outMessageId) {
  if (len > kMaxAppPayload) {
    return false;
  }
  if (len > 0 && payload == nullptr) {
    return false;
  }
  if (ttl == 0) {
    ttl = 1;
  }

  const uint8_t fragmentCount = Fragmenter::CalculateFragmentCount(len);
  if (fragmentCount == 0) {
    return false;
  }

  const uint32_t messageId = nextMessageId();
  for (uint8_t index = 0; index < fragmentCount; ++index) {
    const uint8_t* chunkPtr = nullptr;
    uint16_t chunkLen = 0;
    if (!Fragmenter::GetFragmentSlice(payload, len, index, &chunkPtr, &chunkLen)) {
      return false;
    }

    MeshFrameHeader header{};
    header.magic = kMeshMagic;
    header.version = kMeshVersion;
    header.type = static_cast<uint8_t>(FrameType::Fragment);
    header.originId = nodeId_;
    header.messageId = messageId;
    header.ttl = ttl;
    header.hops = 0;

    FragmentMeta fragmentMeta{};
    fragmentMeta.appType = static_cast<uint8_t>(payloadType);
    fragmentMeta.fragIndex = index;
    fragmentMeta.fragCount = fragmentCount;
    fragmentMeta.totalLen = static_cast<uint16_t>(len);
    fragmentMeta.chunkLen = chunkLen;

    const size_t frameLen = sizeof(MeshFrameHeader) + sizeof(FragmentMeta) + chunkLen;
    if (frameLen > kEspNowMaxPayload) {
      return false;
    }

    uint8_t frame[kEspNowMaxPayload];
    std::memcpy(frame, &header, sizeof(header));
    std::memcpy(frame + sizeof(header), &fragmentMeta, sizeof(fragmentMeta));
    if (chunkLen > 0) {
      std::memcpy(frame + sizeof(header) + sizeof(fragmentMeta), chunkPtr, chunkLen);
    }

    DuplicateKey localFrameKey{};
    localFrameKey.originId = nodeId_;
    localFrameKey.messageId = messageId;
    localFrameKey.frameType = static_cast<uint8_t>(FrameType::Fragment);
    localFrameKey.fragmentIndex = index;
    duplicateFilter_.seenAndRemember(localFrameKey, millis(), kDuplicateWindowMs);

    bool sentAny = false;
    for (uint8_t attempt = 0; attempt < kOriginFrameRepeatCount; ++attempt) {
      if (sendRawBroadcast(frame, frameLen)) {
        sentAny = true;
      }
      if ((attempt + 1) < kOriginFrameRepeatCount) {
        delayRandomRange(kOriginFrameRepeatGapMinMs, kOriginFrameRepeatGapMaxMs);
      }
    }
    if (!sentAny) {
      return false;
    }
    if ((index + 1) < fragmentCount) {
      delayRandomRange(kInterFragmentGapMinMs, kInterFragmentGapMaxMs);
    }
  }

  if (outMessageId != nullptr) {
    *outMessageId = messageId;
  }
  return true;
}

bool EspNowMesh::sendPayloadDirected(AppPayloadType payloadType, const uint8_t* payload, size_t len,
                                     uint32_t dstNodeId, uint8_t ttl, uint32_t* outMessageId) {
  if (dstNodeId == 0 || dstNodeId == nodeId_) {
    return sendPayload(payloadType, payload, len, ttl, outMessageId);
  }
  if (LPWA_ROUTING_MODE <= 0) {
    return sendPayload(payloadType, payload, len, ttl, outMessageId);
  }
  if (len > kMaxAppPayload) {
    return false;
  }
  if (len > 0 && payload == nullptr) {
    return false;
  }
  if (ttl == 0) {
    ttl = 1;
  }

  const uint8_t fragmentCount = Fragmenter::CalculateFragmentCount(len);
  if (fragmentCount == 0) {
    return false;
  }

  const uint32_t messageId = nextMessageId();
  for (uint8_t index = 0; index < fragmentCount; ++index) {
    const uint8_t* chunkPtr = nullptr;
    uint16_t chunkLen = 0;
    if (!Fragmenter::GetFragmentSlice(payload, len, index, &chunkPtr, &chunkLen)) {
      return false;
    }

    MeshFrameHeader header{};
    header.magic = kMeshMagic;
    header.version = kMeshVersion;
    header.type = static_cast<uint8_t>(FrameType::RoutedFragment);
    header.originId = nodeId_;
    header.messageId = messageId;
    header.ttl = ttl;
    header.hops = 0;

    RoutedFragmentMeta routeMeta{};
    routeMeta.dstNodeId = dstNodeId;

    FragmentMeta fragmentMeta{};
    fragmentMeta.appType = static_cast<uint8_t>(payloadType);
    fragmentMeta.fragIndex = index;
    fragmentMeta.fragCount = fragmentCount;
    fragmentMeta.totalLen = static_cast<uint16_t>(len);
    fragmentMeta.chunkLen = chunkLen;

    const size_t frameLen = sizeof(MeshFrameHeader) + sizeof(RoutedFragmentMeta) + sizeof(FragmentMeta) + chunkLen;
    if (frameLen > kEspNowMaxPayload) {
      return false;
    }

    uint8_t frame[kEspNowMaxPayload];
    uint8_t* ptr = frame;
    std::memcpy(ptr, &header, sizeof(header));
    ptr += sizeof(header);
    std::memcpy(ptr, &routeMeta, sizeof(routeMeta));
    ptr += sizeof(routeMeta);
    std::memcpy(ptr, &fragmentMeta, sizeof(fragmentMeta));
    ptr += sizeof(fragmentMeta);
    if (chunkLen > 0) {
      std::memcpy(ptr, chunkPtr, chunkLen);
    }

    DuplicateKey localFrameKey{};
    localFrameKey.originId = nodeId_;
    localFrameKey.messageId = messageId;
    localFrameKey.frameType = static_cast<uint8_t>(FrameType::RoutedFragment);
    localFrameKey.fragmentIndex = index;
    duplicateFilter_.seenAndRemember(localFrameKey, millis(), kDuplicateWindowMs);

    bool sentAny = false;
    RouteEntry route{};
    const bool hasRoute = selectRoute(dstNodeId, &route);
    if (hasRoute) {
      stats_.routeLookupHit++;
    } else {
      stats_.routeLookupMiss++;
    }

    for (uint8_t attempt = 0; attempt < kOriginFrameRepeatCount; ++attempt) {
      bool sentThis = false;
      if (hasRoute) {
        stats_.routedUnicastAttempts++;
        if (sendRawUnicast(route.nextHopMac, frame, frameLen)) {
          stats_.routedUnicastSuccess++;
          sentThis = true;
        } else {
          stats_.routedUnicastFail++;
        }
      }
      if (!sentThis) {
        if (attempt == 0) {
          stats_.routedFallbackFlood++;
        }
        sentThis = sendRawBroadcast(frame, frameLen);
      }
      if (sentThis) {
        sentAny = true;
      }
      if ((attempt + 1) < kOriginFrameRepeatCount) {
        delayRandomRange(kOriginFrameRepeatGapMinMs, kOriginFrameRepeatGapMaxMs);
      }
    }
    if (!sentAny) {
      return false;
    }
    if ((index + 1) < fragmentCount) {
      delayRandomRange(kInterFragmentGapMinMs, kInterFragmentGapMaxMs);
    }
  }

  if (outMessageId != nullptr) {
    *outMessageId = messageId;
  }
  return true;
}

bool EspNowMesh::sendNodeInfo(uint8_t ttl) {
  if (ttl == 0) {
    ttl = 1;
  }

  MeshFrameHeader header{};
  header.magic = kMeshMagic;
  header.version = kMeshVersion;
  header.type = static_cast<uint8_t>(FrameType::NodeInfo);
  header.originId = nodeId_;
  header.messageId = nextMessageId();
  header.ttl = ttl;
  header.hops = 0;

  NodeInfoPayload info{};
  info.nodeId = nodeId_;
  info.uptimeSec = millis() / 1000UL;
  info.freeHeap = ESP.getFreeHeap();
  info.rxFrames = stats_.rxFrames;
  info.txFrames = stats_.txFrames;
  info.seenNodes = nodeCount_;
  if (hasStaMac_) {
    std::memcpy(info.staMac, staMac_, sizeof(info.staMac));
  } else {
    std::memset(info.staMac, 0, sizeof(info.staMac));
  }

  NodeRecord* selfNode = findNode(nodeId_);
  if (selfNode != nullptr) {
    selfNode->lastSeenMs = millis();
    selfNode->hasMac = hasStaMac_;
    if (hasStaMac_) {
      std::memcpy(selfNode->staMac, staMac_, sizeof(selfNode->staMac));
    }
    selfNode->uptimeSec = info.uptimeSec;
    selfNode->freeHeap = info.freeHeap;
    selfNode->remoteRxFrames = info.rxFrames;
    selfNode->remoteTxFrames = info.txFrames;
  }

  const size_t frameLen = sizeof(MeshFrameHeader) + sizeof(NodeInfoPayload);
  uint8_t frame[kEspNowMaxPayload];
  std::memcpy(frame, &header, sizeof(header));
  std::memcpy(frame + sizeof(header), &info, sizeof(info));

  DuplicateKey localFrameKey{};
  localFrameKey.originId = nodeId_;
  localFrameKey.messageId = header.messageId;
  localFrameKey.frameType = static_cast<uint8_t>(FrameType::NodeInfo);
  localFrameKey.fragmentIndex = 0xFF;
  duplicateFilter_.seenAndRemember(localFrameKey, millis(), kDuplicateWindowMs);

  if (!sendRawBroadcast(frame, frameLen)) {
    return false;
  }
  stats_.nodeInfoSent++;
  return true;
}

bool EspNowMesh::sendRawBroadcast(const uint8_t* data, size_t len) {
  return sendRawTo(kBroadcastMac, data, len);
}

bool EspNowMesh::sendRawUnicast(const uint8_t* mac, const uint8_t* data, size_t len) {
  if (mac == nullptr) {
    return false;
  }
  if (!ensurePeerForMac(mac)) {
    return false;
  }
  return sendRawTo(mac, data, len);
}

bool EspNowMesh::sendRawTo(const uint8_t* mac, const uint8_t* data, size_t len) {
  if (mac == nullptr) {
    return false;
  }
  if (data == nullptr || len == 0 || len > kEspNowMaxPayload) {
    return false;
  }

  stats_.txFrames++;
  const uint8_t maxAttempts = static_cast<uint8_t>(kSendRawNoMemRetries + 1U);
  esp_err_t result = ESP_FAIL;
  for (uint8_t attempt = 0; attempt < maxAttempts; ++attempt) {
    result = esp_now_send(mac, data, len);
    if (result == ESP_OK) {
      return true;
    }
    if (result != ESP_ERR_ESPNOW_NO_MEM || (attempt + 1) >= maxAttempts) {
      break;
    }
    stats_.txNoMemRetries++;
    delayRandomRange(kSendRawNoMemBackoffMinMs, kSendRawNoMemBackoffMaxMs);
  }
  if (result == ESP_ERR_ESPNOW_NO_MEM) {
    stats_.txNoMemDrops++;
  }
  stats_.txFailed++;
  return false;
}

bool EspNowMesh::ensurePeerForMac(const uint8_t* mac) {
  if (mac == nullptr) {
    return false;
  }
  if (std::memcmp(mac, kBroadcastMac, 6) == 0) {
    return true;
  }
  if (esp_now_is_peer_exist(mac)) {
    return true;
  }
  esp_now_peer_info_t peerInfo{};
  std::memcpy(peerInfo.peer_addr, mac, 6);
  peerInfo.channel = kMeshChannel;
  peerInfo.encrypt = false;
  peerInfo.ifidx = WIFI_IF_STA;
  const esp_err_t add = esp_now_add_peer(&peerInfo);
  return add == ESP_OK || add == ESP_ERR_ESPNOW_EXIST;
}

void EspNowMesh::pruneRoutingTables(uint32_t nowMs) {
  for (size_t i = 0; i < kMaxNeighborNodes; ++i) {
    NeighborEntry& n = neighbors_[i];
    if (!n.used) {
      continue;
    }
    if ((nowMs - n.lastSeenMs) > kNeighborExpireMs) {
      n = NeighborEntry{};
    }
  }
  for (size_t i = 0; i < kMaxRouteEntries; ++i) {
    RouteEntry& r = routes_[i];
    if (!r.used) {
      continue;
    }
    if ((nowMs - r.learnedMs) > kRouteExpireMs) {
      r = RouteEntry{};
      stats_.routeExpired++;
    }
  }
}

void EspNowMesh::learnRouteFromFrame(uint32_t originId, const uint8_t* senderMac, uint8_t hops, int8_t rssi,
                                     uint32_t nowMs) {
  if (originId == 0 || originId == nodeId_ || senderMac == nullptr) {
    return;
  }

  NeighborEntry* neighbor = upsertNeighbor(senderMac);
  if (neighbor == nullptr) {
    return;
  }
  neighbor->lastSeenMs = nowMs;
  const int16_t sampleQ8 = static_cast<int16_t>(static_cast<int16_t>(rssi) << 8);
  if (neighbor->rssiEwmaQ8 == 0) {
    neighbor->rssiEwmaQ8 = sampleQ8;
  } else {
    neighbor->rssiEwmaQ8 =
        static_cast<int16_t>((static_cast<int32_t>(neighbor->rssiEwmaQ8) * 7 + sampleQ8 + 4) / 8);
  }

  const uint8_t routeHops = (hops >= 0xFE) ? 0xFF : static_cast<uint8_t>(hops + 1);
  const uint16_t metricQ8 = computeRouteMetricQ8(neighbor, routeHops, rssi);
  RouteEntry* route = upsertRoute(originId);
  if (route == nullptr) {
    return;
  }

  const uint8_t zeroMac[6] = {0, 0, 0, 0, 0, 0};
  const bool empty =
      !route->used || route->dstNodeId == 0 || std::memcmp(route->nextHopMac, zeroMac, 6) == 0;
  bool better = empty;
  if (!better) {
    if ((nowMs - route->learnedMs) > kRouteExpireMs) {
      better = true;
    } else if (std::memcmp(route->nextHopMac, senderMac, 6) == 0) {
      better = true;
    } else {
      const uint32_t old = route->metricQ8;
      const uint32_t now = metricQ8;
      better = (now + kRouteHysteresisQ8) < old;
    }
  }
  if (!better) {
    return;
  }

  uint32_t nextHopNodeId = 0;
  (void)resolveNodeIdByMac(senderMac, &nextHopNodeId);

  const bool changed =
      empty || std::memcmp(route->nextHopMac, senderMac, 6) != 0 || route->hops != routeHops ||
      route->metricQ8 != metricQ8 || route->nextHopNodeId != nextHopNodeId;

  route->used = true;
  route->dstNodeId = originId;
  std::memcpy(route->nextHopMac, senderMac, 6);
  route->learnedMs = nowMs;
  route->hops = routeHops;
  route->metricQ8 = metricQ8;
  route->nextHopNodeId = nextHopNodeId;
  if (changed) {
    stats_.routeLearned++;
  }
}

bool EspNowMesh::selectRoute(uint32_t dstNodeId, RouteEntry* outRoute) {
  if (outRoute != nullptr) {
    *outRoute = RouteEntry{};
  }
  if (dstNodeId == 0 || dstNodeId == nodeId_) {
    return false;
  }
  RouteEntry* route = findRoute(dstNodeId);
  if (route == nullptr || !route->used) {
    return false;
  }
  const uint32_t nowMs = millis();
  if ((nowMs - route->learnedMs) > kRouteExpireMs) {
    route->used = false;
    route->dstNodeId = 0;
    stats_.routeExpired++;
    return false;
  }
  const uint8_t zeroMac[6] = {0, 0, 0, 0, 0, 0};
  if (std::memcmp(route->nextHopMac, zeroMac, 6) == 0) {
    route->used = false;
    route->dstNodeId = 0;
    return false;
  }
  if (outRoute != nullptr) {
    *outRoute = *route;
  }
  return true;
}

bool EspNowMesh::lookupNodeMac(uint32_t nodeId, uint8_t* outMac) const {
  if (outMac != nullptr) {
    std::memset(outMac, 0, 6);
  }
  if (nodeId == 0) {
    return false;
  }
  for (size_t i = 0; i < nodeCount_; ++i) {
    if (nodes_[i].nodeId != nodeId || !nodes_[i].hasMac) {
      continue;
    }
    if (outMac != nullptr) {
      std::memcpy(outMac, nodes_[i].staMac, 6);
    }
    return true;
  }
  return false;
}

EspNowMesh::NeighborEntry* EspNowMesh::findNeighbor(const uint8_t* mac) {
  if (mac == nullptr) {
    return nullptr;
  }
  for (size_t i = 0; i < kMaxNeighborNodes; ++i) {
    if (!neighbors_[i].used) {
      continue;
    }
    if (std::memcmp(neighbors_[i].mac, mac, 6) == 0) {
      return &neighbors_[i];
    }
  }
  return nullptr;
}

EspNowMesh::NeighborEntry* EspNowMesh::upsertNeighbor(const uint8_t* mac) {
  if (mac == nullptr) {
    return nullptr;
  }
  NeighborEntry* existing = findNeighbor(mac);
  if (existing != nullptr) {
    return existing;
  }
  size_t target = kMaxNeighborNodes;
  uint32_t oldest = 0;
  const uint32_t nowMs = millis();
  for (size_t i = 0; i < kMaxNeighborNodes; ++i) {
    if (!neighbors_[i].used) {
      target = i;
      break;
    }
    const uint32_t age = nowMs - neighbors_[i].lastSeenMs;
    if (target == kMaxNeighborNodes || age > oldest) {
      oldest = age;
      target = i;
    }
  }
  if (target >= kMaxNeighborNodes) {
    return nullptr;
  }
  neighbors_[target] = NeighborEntry{};
  neighbors_[target].used = true;
  neighbors_[target].lastSeenMs = nowMs;
  neighbors_[target].etxQ8 = 256;
  std::memcpy(neighbors_[target].mac, mac, 6);
  return &neighbors_[target];
}

EspNowMesh::RouteEntry* EspNowMesh::findRoute(uint32_t dstNodeId) {
  for (size_t i = 0; i < kMaxRouteEntries; ++i) {
    if (!routes_[i].used) {
      continue;
    }
    if (routes_[i].dstNodeId == dstNodeId) {
      return &routes_[i];
    }
  }
  return nullptr;
}

EspNowMesh::RouteEntry* EspNowMesh::upsertRoute(uint32_t dstNodeId) {
  if (dstNodeId == 0 || dstNodeId == nodeId_) {
    return nullptr;
  }
  RouteEntry* existing = findRoute(dstNodeId);
  if (existing != nullptr) {
    return existing;
  }

  size_t target = kMaxRouteEntries;
  uint32_t oldest = 0;
  const uint32_t nowMs = millis();
  for (size_t i = 0; i < kMaxRouteEntries; ++i) {
    if (!routes_[i].used) {
      target = i;
      break;
    }
    const uint32_t age = nowMs - routes_[i].learnedMs;
    if (target == kMaxRouteEntries || age > oldest) {
      oldest = age;
      target = i;
    }
  }
  if (target >= kMaxRouteEntries) {
    return nullptr;
  }
  routes_[target] = RouteEntry{};
  routes_[target].used = true;
  routes_[target].dstNodeId = dstNodeId;
  routes_[target].learnedMs = nowMs;
  return &routes_[target];
}

uint16_t EspNowMesh::computeRouteMetricQ8(const NeighborEntry* neighbor, uint8_t hops, int8_t rssi) const {
  const uint16_t etxQ8 = (neighbor != nullptr) ? neighbor->etxQ8 : static_cast<uint16_t>(256);
  const uint32_t hopTerm = static_cast<uint32_t>(hops) * static_cast<uint32_t>(kMetricWeightHopQ8) * 8U;
  const uint32_t etxTerm = (static_cast<uint32_t>(etxQ8) * static_cast<uint32_t>(kMetricWeightEtxQ8)) / 16U;
  const int16_t rssiAbs = static_cast<int16_t>(-rssi);
  const uint32_t rssiTerm =
      (static_cast<uint32_t>((rssiAbs > 0) ? rssiAbs : 0) * static_cast<uint32_t>(kMetricWeightRssiQ8)) / 2U;
  const uint32_t total = hopTerm + etxTerm + rssiTerm;
  if (total > 0xFFFFU) {
    return 0xFFFFU;
  }
  return static_cast<uint16_t>(total);
}

bool EspNowMesh::parseNodeIdString(const char* nodeIdText, uint32_t* outNodeId) {
  if (outNodeId != nullptr) {
    *outNodeId = 0;
  }
  if (nodeIdText == nullptr || nodeIdText[0] == '\0') {
    return false;
  }
  char* endPtr = nullptr;
  const unsigned long parsed = std::strtoul(nodeIdText, &endPtr, 0);
  if (endPtr == nodeIdText || (endPtr != nullptr && *endPtr != '\0') || parsed == 0UL) {
    return false;
  }
  if (outNodeId != nullptr) {
    *outNodeId = static_cast<uint32_t>(parsed);
  }
  return true;
}

bool EspNowMesh::queueInbound(const ReassembledMessage& message) {
  if (inboundCount_ >= kInboundMessageQueueDepth) {
    return false;
  }
  inboundQueue_[inboundTail_] = message;
  inboundTail_ = (inboundTail_ + 1) % kInboundMessageQueueDepth;
  inboundCount_++;
  return true;
}

uint32_t EspNowMesh::nextMessageId() {
  const uint32_t id = nextMessageId_++;
  if (nextMessageId_ == 0) {
    nextMessageId_ = 1;
  }
  return id;
}

NodeRecord* EspNowMesh::findNode(uint32_t nodeId) {
  for (size_t i = 0; i < nodeCount_; ++i) {
    if (nodes_[i].nodeId == nodeId) {
      return &nodes_[i];
    }
  }
  return nullptr;
}

bool EspNowMesh::upsertNode(uint32_t nodeId, NodeRecord** outNode) {
  if (nodeId == 0) {
    if (outNode != nullptr) {
      *outNode = nullptr;
    }
    return false;
  }

  NodeRecord* existing = findNode(nodeId);
  if (existing != nullptr) {
    if (outNode != nullptr) {
      *outNode = existing;
    }
    return true;
  }

  size_t index = 0;
  if (nodeCount_ < kMaxKnownNodes) {
    index = nodeCount_;
    nodeCount_++;
  } else {
    uint32_t nowMs = millis();
    uint32_t oldestAge = 0;
    bool selected = false;
    for (size_t i = 0; i < kMaxKnownNodes; ++i) {
      if (nodes_[i].nodeId == nodeId_) {
        continue;
      }
      const uint32_t age = nowMs - nodes_[i].lastSeenMs;
      if (!selected || age > oldestAge) {
        selected = true;
        oldestAge = age;
        index = i;
      }
    }
    if (!selected) {
      index = 0;
    }
  }

  nodes_[index] = NodeRecord{};
  nodes_[index].nodeId = nodeId;
  nodes_[index].lastSeenMs = millis();

  if (outNode != nullptr) {
    *outNode = &nodes_[index];
  }
  return true;
}

}  // namespace lpwa

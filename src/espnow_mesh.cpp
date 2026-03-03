#include "espnow_mesh.h"

#include <Arduino.h>
#include <WiFi.h>
#include <esp_system.h>
#include <esp_wifi.h>

#include <cstring>

namespace lpwa {

namespace {
constexpr uint8_t kBroadcastMac[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
}

EspNowMesh* EspNowMesh::instance_ = nullptr;

EspNowMesh::EspNowMesh() {
  std::memset(nodes_, 0, sizeof(nodes_));
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
  nodeCount_ = 0;
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

  esp_wifi_set_channel(kMeshChannel, WIFI_SECOND_CHAN_NONE);

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
    selfNode->uptimeSec = 0;
    selfNode->freeHeap = ESP.getFreeHeap();
  }

  lastNodeInfoMs_ = millis();
  return true;
}

void EspNowMesh::loop() {
  processRxQueue();

  uint32_t droppedByTimeout = 0;
  reassembly_.PruneExpired(millis(), kReassemblyTimeoutMs, &droppedByTimeout);
  stats_.reassemblyTimeouts += droppedByTimeout;

  const uint32_t nowMs = millis();
  if ((nowMs - lastNodeInfoMs_) >= kNodeInfoPeriodMs) {
    sendNodeInfo(3);
    lastNodeInfoMs_ = nowMs;
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

void EspNowMesh::onSendStatic(const uint8_t* mac_addr, esp_now_send_status_t status) {
  (void)mac_addr;
  if (instance_ != nullptr) {
    instance_->onSend(status);
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

void EspNowMesh::onSend(esp_now_send_status_t status) {
  if (status == ESP_NOW_SEND_SUCCESS) {
    stats_.txSuccess++;
  } else {
    stats_.txFailed++;
  }
}

void EspNowMesh::onRecv(const uint8_t* mac_addr, int8_t rssi, const uint8_t* data, size_t len) {
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
  while (xQueueReceive(rxQueue_, &item, 0) == pdTRUE) {
    processFrame(item);
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
  }

  const uint32_t nowMs = millis();
  if (duplicateFilter_.seenAndRemember(dedupKey, nowMs, kDuplicateWindowMs)) {
    stats_.droppedDuplicates++;
    return;
  }

  NodeRecord* originNode = nullptr;
  if (upsertNode(header.originId, &originNode) && originNode != nullptr) {
    originNode->lastSeenMs = nowMs;
    originNode->lastRssi = item.rssi;
  }

  if (header.type == static_cast<uint8_t>(FrameType::Fragment)) {
    handleFragmentFrame(header, body, bodyLen, nowMs);
  } else if (header.type == static_cast<uint8_t>(FrameType::NodeInfo)) {
    handleNodeInfoFrame(header, body, bodyLen, item.rssi, nowMs);
  } else {
    stats_.rxParseErrors++;
    return;
  }

  if (header.ttl > 1) {
    uint8_t forwardBuffer[kEspNowMaxPayload];
    if (item.len <= sizeof(forwardBuffer)) {
      std::memcpy(forwardBuffer, item.data, item.len);
      MeshFrameHeader* forwardHeader = reinterpret_cast<MeshFrameHeader*>(forwardBuffer);
      forwardHeader->ttl = static_cast<uint8_t>(forwardHeader->ttl - 1);
      forwardHeader->hops = static_cast<uint8_t>(forwardHeader->hops + 1);
      if (sendRaw(forwardBuffer, item.len)) {
        stats_.forwardedFrames++;
      }
    }
  } else {
    stats_.droppedTtl++;
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
      outHeader->type != static_cast<uint8_t>(FrameType::NodeInfo)) {
    return false;
  }

  *outBody = data + sizeof(MeshFrameHeader);
  *outBodyLen = len - sizeof(MeshFrameHeader);
  return true;
}

void EspNowMesh::handleFragmentFrame(const MeshFrameHeader& header, const uint8_t* body, size_t bodyLen,
                                     uint32_t nowMs) {
  if (body == nullptr || bodyLen < sizeof(FragmentMeta)) {
    stats_.rxParseErrors++;
    return;
  }

  FragmentMeta fragmentMeta{};
  std::memcpy(&fragmentMeta, body, sizeof(FragmentMeta));
  const uint8_t* chunk = body + sizeof(FragmentMeta);
  const size_t chunkLen = bodyLen - sizeof(FragmentMeta);

  if (fragmentMeta.appType != static_cast<uint8_t>(AppPayloadType::Text) &&
      fragmentMeta.appType != static_cast<uint8_t>(AppPayloadType::Binary)) {
    stats_.rxParseErrors++;
    return;
  }
  if (chunkLen != fragmentMeta.chunkLen) {
    stats_.rxParseErrors++;
    return;
  }

  ReassembledMessage completed{};
  const bool done = reassembly_.PushFragment(
      header.originId, header.messageId, static_cast<AppPayloadType>(fragmentMeta.appType), header.hops,
      fragmentMeta.fragIndex, fragmentMeta.fragCount, fragmentMeta.totalLen, chunk,
      fragmentMeta.chunkLen, nowMs, &completed);
  if (!done) {
    return;
  }

  if (queueInbound(completed)) {
    stats_.reassemblyCompleted++;
  } else {
    stats_.rxQueueDropped++;
  }
}

void EspNowMesh::handleNodeInfoFrame(const MeshFrameHeader& header, const uint8_t* body, size_t bodyLen,
                                     int8_t rssi, uint32_t nowMs) {
  (void)header;
  if (body == nullptr || bodyLen < sizeof(NodeInfoPayload)) {
    stats_.rxParseErrors++;
    return;
  }

  NodeInfoPayload remote{};
  std::memcpy(&remote, body, sizeof(remote));
  if (remote.nodeId == 0) {
    stats_.rxParseErrors++;
    return;
  }

  NodeRecord* node = nullptr;
  if (!upsertNode(remote.nodeId, &node) || node == nullptr) {
    return;
  }

  node->lastSeenMs = nowMs;
  node->lastRssi = rssi;
  node->uptimeSec = remote.uptimeSec;
  node->freeHeap = remote.freeHeap;
  node->remoteRxFrames = remote.rxFrames;
  node->remoteTxFrames = remote.txFrames;
  stats_.nodeInfoReceived++;
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

    if (!sendRaw(frame, frameLen)) {
      return false;
    }
    delay(2);
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

  NodeRecord* selfNode = findNode(nodeId_);
  if (selfNode != nullptr) {
    selfNode->lastSeenMs = millis();
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

  if (!sendRaw(frame, frameLen)) {
    return false;
  }
  stats_.nodeInfoSent++;
  return true;
}

bool EspNowMesh::sendRaw(const uint8_t* data, size_t len) {
  if (data == nullptr || len == 0 || len > kEspNowMaxPayload) {
    return false;
  }

  stats_.txFrames++;
  const esp_err_t result = esp_now_send(kBroadcastMac, data, len);
  if (result != ESP_OK) {
    stats_.txFailed++;
    return false;
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


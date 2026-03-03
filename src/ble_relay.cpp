#include "ble_relay.h"

#include <NimBLEDevice.h>

#include <cstring>
#include <string>

namespace lpwa {

namespace {

constexpr uint8_t kBleMagic = 0xC3;
constexpr uint8_t kBleVersion = 1;
constexpr uint8_t kBleTypeText = 1;
constexpr uint32_t kDedupWindowMs = 30000;
constexpr uint32_t kAdvertiseDurationMs = 180;
constexpr uint32_t kAdvertiseGapMs = 20;
constexpr uint8_t kOriginAdvertiseRepeats = 14;

constexpr uint16_t kScanInterval = 200;
constexpr uint16_t kScanWindow = 25;

#pragma pack(push, 1)
struct BleAdvFrameHeader {
  uint8_t magic;
  uint8_t version;
  uint8_t type;
  uint16_t originShort;
  uint16_t messageId;
  uint8_t ttl;
  uint8_t hops;
  uint8_t textLen;
};
#pragma pack(pop)

static_assert((sizeof(BleAdvFrameHeader) + kBleRelayTextMax) <= 27,
              "BLE adv frame exceeds payload budget");

BleRelay* gRelayInstance = nullptr;

}  // namespace

class BleRelayScanCallbacks final : public NimBLEScanCallbacks {
 public:
  void onResult(const NimBLEAdvertisedDevice* device) override {
    if (gRelayInstance == nullptr || device == nullptr) {
      return;
    }

    const std::string manufacturer = device->getManufacturerData();
    if (manufacturer.empty()) {
      return;
    }

    gRelayInstance->onManufacturerData(reinterpret_cast<const uint8_t*>(manufacturer.data()),
                                       manufacturer.size());
  }
};

bool BleRelay::begin(uint32_t nodeId) {
  if (initialized_) {
    return true;
  }

  nodeId_ = nodeId;
  nodeIdShort_ = makeShortNodeId(nodeId_);
  if (nodeIdShort_ == 0) {
    nodeIdShort_ = 1;
  }
  nextMessageId_ = 1;
  stats_ = BleRelayStats{};

  pendingHead_ = 0;
  pendingTail_ = 0;
  pendingCount_ = 0;
  inboundHead_ = 0;
  inboundTail_ = 0;
  inboundCount_ = 0;
  dedupCursor_ = 0;
  scanStarted_ = false;
  advertisingActive_ = false;
  advertisingStopMs_ = 0;
  lastAdvertiseMs_ = 0;

  for (size_t i = 0; i < sizeof(pendingQueue_) / sizeof(pendingQueue_[0]); ++i) {
    pendingQueue_[i] = PendingAdv{};
  }
  for (size_t i = 0; i < sizeof(inboundQueue_) / sizeof(inboundQueue_[0]); ++i) {
    inboundQueue_[i] = BleRelayMessage{};
  }
  for (size_t i = 0; i < sizeof(dedup_) / sizeof(dedup_[0]); ++i) {
    dedup_[i] = DedupEntry{};
  }

  NimBLEDevice::init("");
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);

  scan_ = NimBLEDevice::getScan();
  advertising_ = NimBLEDevice::getAdvertising();
  if (scan_ == nullptr || advertising_ == nullptr) {
    return false;
  }

  if (callbacks_ == nullptr) {
    callbacks_ = new BleRelayScanCallbacks();
  }

  scan_->setScanCallbacks(callbacks_, false);
  scan_->setDuplicateFilter(1);
  scan_->setActiveScan(false);
  scan_->setInterval(kScanInterval);
  scan_->setWindow(kScanWindow);

  gRelayInstance = this;
  initialized_ = true;
  return ensureScanStarted();
}

void BleRelay::loop() {
  if (!initialized_) {
    return;
  }

  ensureScanStarted();
  processAdvQueue(millis());
}

bool BleRelay::sendText(const char* text, uint8_t ttl, uint32_t* outMessageId) {
  stats_.txAttempts++;

  if (!initialized_ || text == nullptr || ttl == 0) {
    stats_.txRejected++;
    return false;
  }

  const size_t len = std::strlen(text);
  if (len == 0 || len > kBleRelayTextMax) {
    stats_.txRejected++;
    return false;
  }

  const uint16_t messageId = nextMessageId_++;
  if (nextMessageId_ == 0) {
    nextMessageId_ = 1;
  }

  const uint32_t nowMs = millis();
  isDuplicateAndRemember(nodeIdShort_, messageId, nowMs);

  PendingAdv pending{};
  pending.used = true;
  pending.originShort = nodeIdShort_;
  pending.messageId = messageId;
  pending.ttl = ttl;
  pending.hops = 0;
  pending.textLen = static_cast<uint8_t>(len);
  std::memcpy(pending.text, text, len);
  pending.text[len] = '\0';

  bool enqueued = false;
  for (uint8_t i = 0; i < kOriginAdvertiseRepeats; ++i) {
    if (enqueuePending(pending)) {
      enqueued = true;
    }
  }

  if (!enqueued) {
    stats_.txRejected++;
    return false;
  }

  if (outMessageId != nullptr) {
    *outMessageId = messageId;
  }
  return true;
}

bool BleRelay::popReceived(BleRelayMessage* outMessage) {
  if (outMessage == nullptr) {
    return false;
  }

  portENTER_CRITICAL(&lock_);
  if (inboundCount_ == 0) {
    portEXIT_CRITICAL(&lock_);
    return false;
  }
  *outMessage = inboundQueue_[inboundHead_];
  inboundHead_ = (inboundHead_ + 1) % kBleRelayQueueDepth;
  inboundCount_--;
  portEXIT_CRITICAL(&lock_);
  return true;
}

void BleRelay::getStats(BleRelayStats* outStats) const {
  if (outStats == nullptr) {
    return;
  }
  *outStats = stats_;
}

bool BleRelay::ensureScanStarted() {
  if (scan_ == nullptr || scanStarted_) {
    return scan_ != nullptr;
  }
  if (!scan_->start(0, false, true)) {
    return false;
  }
  scanStarted_ = true;
  return true;
}

void BleRelay::processAdvQueue(uint32_t nowMs) {
  if (advertising_ == nullptr) {
    return;
  }

  if (advertisingActive_) {
    if ((nowMs - advertisingStopMs_) < 0x80000000UL) {
      advertising_->stop();
      advertisingActive_ = false;
      lastAdvertiseMs_ = nowMs;
    }
    return;
  }

  if ((nowMs - lastAdvertiseMs_) < kAdvertiseGapMs) {
    return;
  }

  PendingAdv pending{};
  if (!dequeuePending(&pending)) {
    return;
  }

  if (!publishFrame(pending)) {
    stats_.txRejected++;
    return;
  }

  advertisingActive_ = true;
  advertisingStopMs_ = nowMs + kAdvertiseDurationMs;
}

bool BleRelay::enqueuePending(const PendingAdv& pending) {
  portENTER_CRITICAL(&lock_);
  if (pendingCount_ >= kBleRelayQueueDepth) {
    portEXIT_CRITICAL(&lock_);
    return false;
  }
  pendingQueue_[pendingTail_] = pending;
  pendingTail_ = (pendingTail_ + 1) % kBleRelayQueueDepth;
  pendingCount_++;
  portEXIT_CRITICAL(&lock_);
  return true;
}

bool BleRelay::dequeuePending(PendingAdv* outPending) {
  if (outPending == nullptr) {
    return false;
  }

  portENTER_CRITICAL(&lock_);
  if (pendingCount_ == 0) {
    portEXIT_CRITICAL(&lock_);
    return false;
  }
  *outPending = pendingQueue_[pendingHead_];
  pendingHead_ = (pendingHead_ + 1) % kBleRelayQueueDepth;
  pendingCount_--;
  portEXIT_CRITICAL(&lock_);
  return true;
}

bool BleRelay::enqueueReceived(const BleRelayMessage& message) {
  portENTER_CRITICAL(&lock_);
  if (inboundCount_ >= kBleRelayQueueDepth) {
    portEXIT_CRITICAL(&lock_);
    return false;
  }
  inboundQueue_[inboundTail_] = message;
  inboundTail_ = (inboundTail_ + 1) % kBleRelayQueueDepth;
  inboundCount_++;
  portEXIT_CRITICAL(&lock_);
  return true;
}

bool BleRelay::isDuplicateAndRemember(uint16_t originShort, uint16_t messageId, uint32_t nowMs) {
  portENTER_CRITICAL(&lock_);
  for (size_t i = 0; i < sizeof(dedup_) / sizeof(dedup_[0]); ++i) {
    DedupEntry& e = dedup_[i];
    if (!e.used) {
      continue;
    }
    if ((nowMs - e.firstSeenMs) > kDedupWindowMs) {
      e.used = false;
      continue;
    }
    if (e.originShort == originShort && e.messageId == messageId) {
      portEXIT_CRITICAL(&lock_);
      return true;
    }
  }

  DedupEntry& slot = dedup_[dedupCursor_];
  slot.used = true;
  slot.originShort = originShort;
  slot.messageId = messageId;
  slot.firstSeenMs = nowMs;
  dedupCursor_ = static_cast<uint8_t>((dedupCursor_ + 1) % (sizeof(dedup_) / sizeof(dedup_[0])));
  portEXIT_CRITICAL(&lock_);
  return false;
}

bool BleRelay::publishFrame(const PendingAdv& pending) {
  BleAdvFrameHeader header{};
  header.magic = kBleMagic;
  header.version = kBleVersion;
  header.type = kBleTypeText;
  header.originShort = pending.originShort;
  header.messageId = pending.messageId;
  header.ttl = pending.ttl;
  header.hops = pending.hops;
  header.textLen = pending.textLen;

  uint8_t payload[sizeof(BleAdvFrameHeader) + kBleRelayTextMax]{};
  std::memcpy(payload, &header, sizeof(header));
  std::memcpy(payload + sizeof(header), pending.text, pending.textLen);
  const size_t payloadLen = sizeof(header) + pending.textLen;

  NimBLEAdvertisementData advData;
  advData.setFlags(BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP);
  advData.setManufacturerData(std::string(reinterpret_cast<const char*>(payload), payloadLen));

  if (!advertising_->setAdvertisementData(advData)) {
    return false;
  }
  if (!advertising_->start()) {
    return false;
  }
  return true;
}

void BleRelay::onManufacturerData(const uint8_t* payload, size_t len) {
  if (!initialized_ || payload == nullptr || len < sizeof(BleAdvFrameHeader)) {
    return;
  }

  BleAdvFrameHeader header{};
  std::memcpy(&header, payload, sizeof(header));
  if (header.magic != kBleMagic || header.version != kBleVersion || header.type != kBleTypeText) {
    return;
  }
  if (header.textLen == 0 || header.textLen > kBleRelayTextMax) {
    return;
  }
  if (len < (sizeof(BleAdvFrameHeader) + header.textLen)) {
    return;
  }

  const uint32_t nowMs = millis();
  if (isDuplicateAndRemember(header.originShort, header.messageId, nowMs)) {
    stats_.droppedDuplicates++;
    return;
  }

  BleRelayMessage message{};
  message.originId = header.originShort;
  message.messageId = header.messageId;
  message.ttl = header.ttl;
  message.hops = header.hops;
  std::memcpy(message.text, payload + sizeof(BleAdvFrameHeader), header.textLen);
  message.text[header.textLen] = '\0';

  if (enqueueReceived(message)) {
    stats_.rxMessages++;
  } else {
    stats_.txRejected++;
  }

  if (header.ttl <= 1) {
    return;
  }

  PendingAdv forward{};
  forward.used = true;
  forward.originShort = header.originShort;
  forward.messageId = header.messageId;
  forward.ttl = static_cast<uint8_t>(header.ttl - 1);
  forward.hops = static_cast<uint8_t>(header.hops + 1);
  forward.textLen = header.textLen;
  std::memcpy(forward.text, payload + sizeof(BleAdvFrameHeader), header.textLen);
  forward.text[header.textLen] = '\0';

  if (enqueuePending(forward)) {
    stats_.forwarded++;
  }
}

uint16_t BleRelay::makeShortNodeId(uint32_t nodeId) {
  return static_cast<uint16_t>((nodeId >> 16) ^ (nodeId & 0xFFFFU));
}

}  // namespace lpwa

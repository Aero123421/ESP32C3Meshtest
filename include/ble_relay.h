#pragma once

#include <Arduino.h>
#include <freertos/FreeRTOS.h>

class NimBLEScan;
class NimBLEAdvertising;

namespace lpwa {

class BleRelayScanCallbacks;

constexpr size_t kBleRelayTextMax = 16;
constexpr size_t kBleRelayQueueDepth = 12;

struct BleRelayMessage {
  uint32_t originId = 0;
  uint32_t messageId = 0;
  uint8_t ttl = 0;
  uint8_t hops = 0;
  char text[kBleRelayTextMax + 1]{};
};

struct BleRelayStats {
  uint32_t txAttempts = 0;
  uint32_t txRejected = 0;
  uint32_t rxMessages = 0;
  uint32_t droppedDuplicates = 0;
  uint32_t forwarded = 0;
  uint32_t notImplemented = 0;
};

class BleRelay {
 public:
  BleRelay() = default;
  ~BleRelay() = default;

  bool begin(uint32_t nodeId);
  void loop();

  bool sendText(const char* text, uint8_t ttl, uint32_t* outMessageId);
  bool popReceived(BleRelayMessage* outMessage);
  void getStats(BleRelayStats* outStats) const;

 private:
  friend class BleRelayScanCallbacks;

  struct DedupEntry {
    bool used = false;
    uint16_t originShort = 0;
    uint16_t messageId = 0;
    uint32_t firstSeenMs = 0;
  };

  struct PendingAdv {
    bool used = false;
    uint16_t originShort = 0;
    uint16_t messageId = 0;
    uint8_t ttl = 0;
    uint8_t hops = 0;
    uint8_t textLen = 0;
    char text[kBleRelayTextMax + 1]{};
  };

  bool ensureScanStarted();
  void processAdvQueue(uint32_t nowMs);
  bool enqueuePending(const PendingAdv& pending);
  bool dequeuePending(PendingAdv* outPending);

  bool enqueueReceived(const BleRelayMessage& message);
  bool isDuplicateAndRemember(uint16_t originShort, uint16_t messageId, uint32_t nowMs);
  bool publishFrame(const PendingAdv& pending);

  void onManufacturerData(const uint8_t* payload, size_t len);
  static uint16_t makeShortNodeId(uint32_t nodeId);

  portMUX_TYPE lock_ = portMUX_INITIALIZER_UNLOCKED;
  bool initialized_ = false;
  uint32_t nodeId_ = 0;
  uint16_t nodeIdShort_ = 0;
  uint16_t nextMessageId_ = 1;
  BleRelayStats stats_{};

  NimBLEScan* scan_ = nullptr;
  NimBLEAdvertising* advertising_ = nullptr;
  BleRelayScanCallbacks* callbacks_ = nullptr;

  PendingAdv pendingQueue_[kBleRelayQueueDepth]{};
  uint8_t pendingHead_ = 0;
  uint8_t pendingTail_ = 0;
  uint8_t pendingCount_ = 0;

  BleRelayMessage inboundQueue_[kBleRelayQueueDepth]{};
  uint8_t inboundHead_ = 0;
  uint8_t inboundTail_ = 0;
  uint8_t inboundCount_ = 0;

  DedupEntry dedup_[96]{};
  uint8_t dedupCursor_ = 0;

  bool scanStarted_ = false;
  bool advertisingActive_ = false;
  uint32_t advertisingStopMs_ = 0;
  uint32_t lastAdvertiseMs_ = 0;
};

}  // namespace lpwa

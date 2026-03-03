#pragma once

#include <Arduino.h>

#include "mesh_protocol.h"

namespace lpwa {

struct ReassembledMessage {
  uint32_t originId = 0;
  uint32_t messageId = 0;
  AppPayloadType payloadType = AppPayloadType::Text;
  uint8_t hops = 0;
  uint16_t length = 0;
  uint8_t data[kMaxAppPayload]{};
};

class Fragmenter {
 public:
  static uint8_t CalculateFragmentCount(size_t totalLen);
  static bool GetFragmentSlice(const uint8_t* payload, size_t totalLen, uint8_t index,
                               const uint8_t** outPtr, uint16_t* outLen);
};

class ReassemblyManager {
 public:
  ReassemblyManager();

  bool PushFragment(uint32_t originId, uint32_t messageId, AppPayloadType payloadType, uint8_t hops,
                    uint8_t fragIndex, uint8_t fragCount, uint16_t totalLen, const uint8_t* chunk,
                    uint16_t chunkLen, uint32_t nowMs, ReassembledMessage* outMessage);

  void PruneExpired(uint32_t nowMs, uint32_t timeoutMs, uint32_t* droppedCount);

 private:
  static constexpr size_t kSlotCount = 16;

  struct Slot {
    bool active = false;
    uint32_t originId = 0;
    uint32_t messageId = 0;
    AppPayloadType payloadType = AppPayloadType::Text;
    uint8_t hops = 0;
    uint8_t fragCount = 0;
    uint16_t totalLen = 0;
    uint32_t receivedMask = 0;
    uint32_t lastUpdateMs = 0;
    uint8_t data[kMaxAppPayload]{};
  };

  Slot* findSlot(uint32_t originId, uint32_t messageId, AppPayloadType payloadType);
  Slot* allocateSlot(uint32_t nowMs);
  static void resetSlot(Slot* slot);

  Slot slots_[kSlotCount];
};

}  // namespace lpwa

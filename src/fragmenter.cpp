#include "fragmenter.h"

#include <algorithm>
#include <cstring>

namespace lpwa {

uint8_t Fragmenter::CalculateFragmentCount(size_t totalLen) {
  if (totalLen > kMaxAppPayload) {
    return 0;
  }
  if (totalLen == 0) {
    return 1;
  }
  const size_t count = (totalLen + kFragmentChunkSize - 1) / kFragmentChunkSize;
  return static_cast<uint8_t>(count);
}

bool Fragmenter::GetFragmentSlice(const uint8_t* payload, size_t totalLen, uint8_t index,
                                  const uint8_t** outPtr, uint16_t* outLen) {
  if (outPtr == nullptr || outLen == nullptr) {
    return false;
  }

  const uint8_t fragmentCount = CalculateFragmentCount(totalLen);
  if (fragmentCount == 0 || index >= fragmentCount) {
    return false;
  }

  if (totalLen == 0) {
    *outPtr = payload;
    *outLen = 0;
    return true;
  }

  if (payload == nullptr) {
    return false;
  }

  const size_t offset = static_cast<size_t>(index) * kFragmentChunkSize;
  const size_t remaining = totalLen - offset;
  const size_t chunkLen = std::min(remaining, kFragmentChunkSize);

  *outPtr = payload + offset;
  *outLen = static_cast<uint16_t>(chunkLen);
  return true;
}

ReassemblyManager::ReassemblyManager() {
  for (size_t i = 0; i < kSlotCount; ++i) {
    resetSlot(&slots_[i]);
  }
}

bool ReassemblyManager::PushFragment(uint32_t originId, uint32_t messageId, AppPayloadType payloadType,
                                     uint8_t hops, uint8_t fragIndex, uint8_t fragCount,
                                     uint16_t totalLen, const uint8_t* chunk, uint16_t chunkLen,
                                     uint32_t nowMs, ReassembledMessage* outMessage) {
  if (outMessage == nullptr) {
    return false;
  }
  if (fragCount == 0 || fragCount > kMaxFragments || fragIndex >= fragCount) {
    return false;
  }
  if (totalLen > kMaxAppPayload) {
    return false;
  }
  if (chunkLen > kFragmentChunkSize) {
    return false;
  }
  if (chunkLen > 0 && chunk == nullptr) {
    return false;
  }

  const size_t offset = static_cast<size_t>(fragIndex) * kFragmentChunkSize;
  if ((offset + chunkLen) > totalLen) {
    return false;
  }

  Slot* slot = findSlot(originId, messageId, payloadType);
  if (slot == nullptr) {
    slot = allocateSlot(nowMs);
    if (slot == nullptr) {
      return false;
    }

    slot->active = true;
    slot->originId = originId;
    slot->messageId = messageId;
    slot->payloadType = payloadType;
    slot->fragCount = fragCount;
    slot->totalLen = totalLen;
    slot->receivedMask = 0;
    slot->hops = hops;
    slot->lastUpdateMs = nowMs;
  } else {
    if (slot->fragCount != fragCount || slot->totalLen != totalLen) {
      return false;
    }
    if (hops > slot->hops) {
      slot->hops = hops;
    }
  }

  const uint32_t bit = (1UL << fragIndex);
  if ((slot->receivedMask & bit) != 0) {
    return false;
  }

  if (chunkLen > 0) {
    std::memcpy(slot->data + offset, chunk, chunkLen);
  }
  slot->receivedMask |= bit;
  slot->lastUpdateMs = nowMs;

  const uint32_t completeMask = (slot->fragCount == 32) ? 0xFFFFFFFFUL : ((1UL << slot->fragCount) - 1UL);
  if ((slot->receivedMask & completeMask) != completeMask) {
    return false;
  }

  outMessage->originId = slot->originId;
  outMessage->messageId = slot->messageId;
  outMessage->payloadType = slot->payloadType;
  outMessage->hops = slot->hops;
  outMessage->length = slot->totalLen;
  if (slot->totalLen > 0) {
    std::memcpy(outMessage->data, slot->data, slot->totalLen);
  }

  resetSlot(slot);
  return true;
}

void ReassemblyManager::PruneExpired(uint32_t nowMs, uint32_t timeoutMs, uint32_t* droppedCount) {
  for (size_t i = 0; i < kSlotCount; ++i) {
    Slot* slot = &slots_[i];
    if (!slot->active) {
      continue;
    }
    if ((nowMs - slot->lastUpdateMs) <= timeoutMs) {
      continue;
    }
    resetSlot(slot);
    if (droppedCount != nullptr) {
      (*droppedCount)++;
    }
  }
}

ReassemblyManager::Slot* ReassemblyManager::findSlot(uint32_t originId, uint32_t messageId,
                                                     AppPayloadType payloadType) {
  for (size_t i = 0; i < kSlotCount; ++i) {
    Slot* slot = &slots_[i];
    if (!slot->active) {
      continue;
    }
    if (slot->originId == originId && slot->messageId == messageId && slot->payloadType == payloadType) {
      return slot;
    }
  }
  return nullptr;
}

ReassemblyManager::Slot* ReassemblyManager::allocateSlot(uint32_t nowMs) {
  for (size_t i = 0; i < kSlotCount; ++i) {
    if (!slots_[i].active) {
      return &slots_[i];
    }
  }

  size_t oldestIndex = 0;
  uint32_t oldestAge = 0;
  for (size_t i = 0; i < kSlotCount; ++i) {
    const uint32_t age = nowMs - slots_[i].lastUpdateMs;
    if (age > oldestAge) {
      oldestAge = age;
      oldestIndex = i;
    }
  }
  resetSlot(&slots_[oldestIndex]);
  return &slots_[oldestIndex];
}

void ReassemblyManager::resetSlot(Slot* slot) {
  if (slot == nullptr) {
    return;
  }
  slot->active = false;
  slot->originId = 0;
  slot->messageId = 0;
  slot->payloadType = AppPayloadType::Text;
  slot->hops = 0;
  slot->fragCount = 0;
  slot->totalLen = 0;
  slot->receivedMask = 0;
  slot->lastUpdateMs = 0;
}

}  // namespace lpwa


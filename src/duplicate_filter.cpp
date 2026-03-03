#include "duplicate_filter.h"

namespace lpwa {

bool DuplicateFilter::equals(const DuplicateKey& a, const DuplicateKey& b) {
  return a.originId == b.originId && a.messageId == b.messageId && a.frameType == b.frameType &&
         a.fragmentIndex == b.fragmentIndex;
}

bool DuplicateFilter::seen(const DuplicateKey& key, uint32_t nowMs, uint32_t windowMs) {
  for (size_t i = 0; i < kCapacity; ++i) {
    Entry& entry = entries_[i];
    if (!entry.used) {
      continue;
    }

    if ((nowMs - entry.firstSeenMs) > windowMs) {
      entry.used = false;
      continue;
    }

    if (equals(entry.key, key)) {
      return true;
    }
  }
  return false;
}

void DuplicateFilter::remember(const DuplicateKey& key, uint32_t nowMs) {
  Entry& slot = entries_[nextInsert_];
  slot.key = key;
  slot.firstSeenMs = nowMs;
  slot.used = true;

  nextInsert_ = (nextInsert_ + 1) % kCapacity;
}

bool DuplicateFilter::seenAndRemember(const DuplicateKey& key, uint32_t nowMs, uint32_t windowMs) {
  if (seen(key, nowMs, windowMs)) {
    return true;
  }
  remember(key, nowMs);
  return false;
}

void DuplicateFilter::clear() {
  for (size_t i = 0; i < kCapacity; ++i) {
    entries_[i].used = false;
    entries_[i].firstSeenMs = 0;
  }
  nextInsert_ = 0;
}

}  // namespace lpwa

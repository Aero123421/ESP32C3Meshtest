#pragma once

#include <Arduino.h>

namespace lpwa {

struct DuplicateKey {
  uint32_t originId = 0;
  uint32_t messageId = 0;
  uint8_t frameType = 0;
  uint8_t fragmentIndex = 0;
};

class DuplicateFilter {
 public:
  bool seenAndRemember(const DuplicateKey& key, uint32_t nowMs, uint32_t windowMs);
  void clear();

 private:
  static constexpr size_t kCapacity = 192;

  struct Entry {
    DuplicateKey key{};
    uint32_t firstSeenMs = 0;
    bool used = false;
  };

  static bool equals(const DuplicateKey& a, const DuplicateKey& b);

  Entry entries_[kCapacity];
  size_t nextInsert_ = 0;
};

}  // namespace lpwa


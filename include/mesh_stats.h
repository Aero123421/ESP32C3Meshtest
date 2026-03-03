#pragma once

#include <Arduino.h>

namespace lpwa {

struct MeshStats {
  uint32_t txFrames = 0;
  uint32_t txSuccess = 0;
  uint32_t txFailed = 0;
  uint32_t txNoMemRetries = 0;
  uint32_t txNoMemDrops = 0;
  uint32_t rxFrames = 0;
  uint32_t rxQueueDropped = 0;
  uint32_t rxParseErrors = 0;
  uint32_t forwardedFrames = 0;
  uint32_t droppedDuplicates = 0;
  uint32_t droppedTtl = 0;
  uint32_t reassemblyCompleted = 0;
  uint32_t reassemblyTimeouts = 0;
  uint32_t nodeInfoSent = 0;
  uint32_t nodeInfoReceived = 0;
};

struct NodeRecord {
  uint32_t nodeId = 0;
  uint32_t lastSeenMs = 0;
  int8_t lastRssi = 0;
  bool hasMac = false;
  uint8_t staMac[6]{};
  uint32_t uptimeSec = 0;
  uint32_t freeHeap = 0;
  uint32_t remoteRxFrames = 0;
  uint32_t remoteTxFrames = 0;
};

}  // namespace lpwa

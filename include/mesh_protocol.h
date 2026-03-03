#pragma once

#include <Arduino.h>

namespace lpwa {

constexpr uint16_t kMeshMagic = 0x4C50;
constexpr uint8_t kMeshVersion = 1;
constexpr uint8_t kDefaultTtl = 10;
constexpr uint8_t kMeshChannel = 1;

constexpr size_t kEspNowMaxPayload = 250;
constexpr size_t kFragmentChunkSize = 180;
constexpr size_t kMaxAppPayload = 1024;
constexpr size_t kMaxFragments = (kMaxAppPayload + kFragmentChunkSize - 1) / kFragmentChunkSize;

constexpr uint32_t kNodeInfoPeriodMs = 10000;
constexpr uint32_t kDuplicateWindowMs = 30000;
constexpr uint32_t kReassemblyTimeoutMs = 15000;
constexpr uint8_t kOriginFrameRepeatCount = 3;
constexpr uint8_t kOriginFrameRepeatGapMs = 4;
constexpr uint8_t kForwardJitterMinMs = 2;
constexpr uint8_t kForwardJitterMaxMs = 9;
constexpr uint8_t kForwardSendAttempts = 2;
constexpr uint8_t kRxProcessBudgetPerLoop = 24;

constexpr size_t kMaxKnownNodes = 32;
constexpr size_t kInboundMessageQueueDepth = 32;
constexpr size_t kRxQueueDepth = 96;

enum class FrameType : uint8_t {
  Fragment = 1,
  NodeInfo = 2,
};

enum class AppPayloadType : uint8_t {
  Text = 1,
  Binary = 2,
};

#pragma pack(push, 1)
struct MeshFrameHeader {
  uint16_t magic;
  uint8_t version;
  uint8_t type;
  uint32_t originId;
  uint32_t messageId;
  uint8_t ttl;
  uint8_t hops;
};

struct FragmentMeta {
  uint8_t appType;
  uint8_t fragIndex;
  uint8_t fragCount;
  uint8_t reserved;
  uint16_t totalLen;
  uint16_t chunkLen;
};

struct NodeInfoPayload {
  uint32_t nodeId;
  uint32_t uptimeSec;
  uint32_t freeHeap;
  uint32_t rxFrames;
  uint32_t txFrames;
  uint16_t seenNodes;
  uint8_t staMac[6];
};
#pragma pack(pop)

static_assert(sizeof(MeshFrameHeader) == 14, "MeshFrameHeader size mismatch");
static_assert(sizeof(FragmentMeta) == 8, "FragmentMeta size mismatch");
static_assert(sizeof(NodeInfoPayload) == 28, "NodeInfoPayload size mismatch");

}  // namespace lpwa

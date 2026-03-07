#pragma once

#include <Arduino.h>

namespace lpwa {

#ifndef LPWA_MESH_CHANNEL
#define LPWA_MESH_CHANNEL 1
#endif

#ifndef LPWA_ENABLE_WIFI_LR
#define LPWA_ENABLE_WIFI_LR 1
#endif

#ifndef LPWA_ENABLE_BLE_RELAY
#define LPWA_ENABLE_BLE_RELAY 1
#endif

#ifndef LPWA_ALLOW_WIFI_LR_WITH_BLE
#define LPWA_ALLOW_WIFI_LR_WITH_BLE 0
#endif

#ifndef LPWA_ROUTING_MODE
#define LPWA_ROUTING_MODE 2
#endif

#ifndef LPWA_ROBUST_MODE
#define LPWA_ROBUST_MODE 1
#endif
#if (LPWA_ROBUST_MODE != 0) && (LPWA_ROBUST_MODE != 1)
#error "LPWA_ROBUST_MODE must be 0 or 1"
#endif

#ifndef LPWA_MESH_TX_POWER_QDBM
#define LPWA_MESH_TX_POWER_QDBM 72
#endif

constexpr uint16_t kMeshMagic = 0x4C50;
constexpr uint8_t kMeshVersion = 1;
#if LPWA_ROBUST_MODE
constexpr uint8_t kDefaultTtl = 12;
#else
constexpr uint8_t kDefaultTtl = 10;
#endif
constexpr uint8_t kMaxTtl = 14;
constexpr uint8_t kMeshChannel = LPWA_MESH_CHANNEL;
constexpr int8_t kMeshTxPowerQuarterDbm = LPWA_MESH_TX_POWER_QDBM;
#if LPWA_ENABLE_BLE_RELAY
// Stability-first default: keep BLE coexistence mode as baseline.
// Note: LPWA_ALLOW_WIFI_LR_WITH_BLE=0 の場合、runtime long_range も無効。
constexpr bool kWifiLongRangeDefault = false;
#elif LPWA_ENABLE_WIFI_LR
constexpr bool kWifiLongRangeDefault = true;
#else
constexpr bool kWifiLongRangeDefault = false;
#endif
#if LPWA_ENABLE_BLE_RELAY
constexpr bool kBleRelayDefault = true;
#else
constexpr bool kBleRelayDefault = false;
#endif

constexpr size_t kEspNowMaxPayload = 250;
constexpr size_t kFragmentChunkSize = 180;
constexpr size_t kMaxAppPayload = 1024;
constexpr size_t kMaxFragments = (kMaxAppPayload + kFragmentChunkSize - 1) / kFragmentChunkSize;

constexpr uint32_t kNodeInfoPeriodMs = 10000;
constexpr uint32_t kNodeInfoPeriodMinMs = 3000;
constexpr uint32_t kNodeInfoPeriodMaxMs = 120000;
constexpr uint8_t kDefaultNodeInfoTtl = 5;
constexpr uint16_t kNodeInfoInitialJitterMinMs = 800;
constexpr uint16_t kNodeInfoInitialJitterMaxMs = 4200;
constexpr uint16_t kNodeInfoJitterMaxMs = 1800;
constexpr uint32_t kRouteExpireMs = 45000;
constexpr uint32_t kNeighborExpireMs = 60000;
constexpr uint16_t kRouteHysteresisQ8 = 48;  // 0.1875
constexpr uint8_t kMetricWeightHopQ8 = 32;   // 0.125
constexpr uint8_t kMetricWeightEtxQ8 = 128;  // 0.5
constexpr uint8_t kMetricWeightRssiQ8 = 8;   // 0.03125
constexpr uint32_t kDuplicateWindowMs = 30000;
constexpr uint32_t kReassemblyTimeoutMs = 22000;
#if LPWA_ENABLE_BLE_RELAY
constexpr uint8_t kOriginFrameRepeatCount = 5;
#elif LPWA_ROBUST_MODE
constexpr uint8_t kOriginFrameRepeatCount = 4;
#else
constexpr uint8_t kOriginFrameRepeatCount = 3;
#endif
constexpr uint8_t kDirectedOriginAttemptCount = 2;
constexpr uint8_t kDirectedFallbackFloodAttempts = 1;
constexpr uint8_t kOriginFrameRepeatGapMinMs = 6;
constexpr uint8_t kOriginFrameRepeatGapMaxMs = 14;
constexpr uint8_t kInterFragmentGapMinMs = 7;
constexpr uint8_t kInterFragmentGapMaxMs = 16;
constexpr uint8_t kForwardJitterMinMs = 10;
constexpr uint8_t kForwardJitterMaxMs = 34;
#if LPWA_ROBUST_MODE
constexpr uint8_t kForwardSendAttemptsFragment = 4;
#else
constexpr uint8_t kForwardSendAttemptsFragment = 3;
#endif
constexpr uint8_t kForwardSendAttemptsNodeInfo = 1;
constexpr uint8_t kSendRawNoMemRetries = 2;
constexpr uint8_t kSendRawNoMemBackoffMinMs = 2;
constexpr uint8_t kSendRawNoMemBackoffMaxMs = 8;
constexpr uint8_t kRxProcessBudgetPerLoop = 40;
constexpr uint8_t kAdaptiveAttemptMax = 5;
constexpr uint16_t kAdaptiveQueueHighWater = 96;
constexpr uint16_t kAdaptiveQueueLowWater = 24;

constexpr size_t kMaxKnownNodes = 48;
constexpr size_t kMaxNeighborNodes = 48;
constexpr size_t kMaxRouteEntries = 96;
constexpr size_t kInboundMessageQueueDepth = 48;
constexpr size_t kRxQueueDepth = 128;

enum class FrameType : uint8_t {
  Fragment = 1,
  NodeInfo = 2,
  RoutedFragment = 3,
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

struct RoutedFragmentMeta {
  uint32_t dstNodeId;
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
static_assert(sizeof(RoutedFragmentMeta) == 4, "RoutedFragmentMeta size mismatch");
static_assert(sizeof(NodeInfoPayload) == 28, "NodeInfoPayload size mismatch");
static_assert(kMeshChannel >= 1 && kMeshChannel <= 14, "LPWA_MESH_CHANNEL must be 1..14");
static_assert(kMeshTxPowerQuarterDbm >= 8 && kMeshTxPowerQuarterDbm <= 84,
              "LPWA_MESH_TX_POWER_QDBM must be 8..84");
static_assert(kAdaptiveQueueLowWater < kAdaptiveQueueHighWater,
              "Adaptive queue watermarks must satisfy low < high");
static_assert(kAdaptiveQueueHighWater < kRxQueueDepth, "Adaptive high watermark must be less than RX queue depth");

}  // namespace lpwa

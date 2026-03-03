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

#ifndef LPWA_MESH_TX_POWER_QDBM
#define LPWA_MESH_TX_POWER_QDBM 84
#endif

constexpr uint16_t kMeshMagic = 0x4C50;
constexpr uint8_t kMeshVersion = 1;
constexpr uint8_t kDefaultTtl = 10;
constexpr uint8_t kMeshChannel = LPWA_MESH_CHANNEL;
constexpr int8_t kMeshTxPowerQuarterDbm = LPWA_MESH_TX_POWER_QDBM;
#if LPWA_ENABLE_WIFI_LR && (!LPWA_ENABLE_BLE_RELAY || LPWA_ALLOW_WIFI_LR_WITH_BLE)
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

constexpr uint32_t kNodeInfoPeriodMs = 15000;
constexpr uint16_t kNodeInfoInitialJitterMinMs = 800;
constexpr uint16_t kNodeInfoInitialJitterMaxMs = 4200;
constexpr uint16_t kNodeInfoJitterMaxMs = 1800;
constexpr uint32_t kDuplicateWindowMs = 30000;
constexpr uint32_t kReassemblyTimeoutMs = 22000;
#if LPWA_ENABLE_BLE_RELAY
constexpr uint8_t kOriginFrameRepeatCount = 5;
#else
constexpr uint8_t kOriginFrameRepeatCount = 3;
#endif
constexpr uint8_t kOriginFrameRepeatGapMinMs = 4;
constexpr uint8_t kOriginFrameRepeatGapMaxMs = 10;
constexpr uint8_t kInterFragmentGapMinMs = 5;
constexpr uint8_t kInterFragmentGapMaxMs = 12;
constexpr uint8_t kForwardJitterMinMs = 8;
constexpr uint8_t kForwardJitterMaxMs = 28;
constexpr uint8_t kForwardSendAttemptsFragment = 3;
constexpr uint8_t kForwardSendAttemptsNodeInfo = 1;
constexpr uint8_t kSendRawNoMemRetries = 2;
constexpr uint8_t kSendRawNoMemBackoffMinMs = 2;
constexpr uint8_t kSendRawNoMemBackoffMaxMs = 8;
constexpr uint8_t kRxProcessBudgetPerLoop = 40;

constexpr size_t kMaxKnownNodes = 32;
constexpr size_t kInboundMessageQueueDepth = 32;
constexpr size_t kRxQueueDepth = 128;

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
static_assert(kMeshChannel >= 1 && kMeshChannel <= 14, "LPWA_MESH_CHANNEL must be 1..14");
static_assert(kMeshTxPowerQuarterDbm >= 8 && kMeshTxPowerQuarterDbm <= 84,
              "LPWA_MESH_TX_POWER_QDBM must be 8..84");

}  // namespace lpwa

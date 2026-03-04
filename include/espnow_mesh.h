#pragma once

#include <Arduino.h>
#include <esp_now.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#if __has_include(<esp_arduino_version.h>)
#include <esp_arduino_version.h>
#endif
#ifndef ESP_ARDUINO_VERSION_MAJOR
#define ESP_ARDUINO_VERSION_MAJOR 2
#endif

#include "duplicate_filter.h"
#include "fragmenter.h"
#include "mesh_protocol.h"
#include "mesh_stats.h"

namespace lpwa {

class EspNowMesh {
 public:
  EspNowMesh();
  ~EspNowMesh() = default;

  bool begin();
  void loop();

  bool sendText(const char* text, uint8_t ttl, uint32_t* outMessageId);
  bool sendBinary(const uint8_t* payload, size_t len, uint8_t ttl, uint32_t* outMessageId);
  bool sendTextDirected(const char* text, uint32_t dstNodeId, uint8_t ttl, uint32_t* outMessageId);
  bool sendBinaryDirected(const uint8_t* payload, size_t len, uint32_t dstNodeId, uint8_t ttl,
                          uint32_t* outMessageId);

  bool popReceivedMessage(ReassembledMessage* outMessage);

  void getStats(MeshStats* outStats) const;
  size_t copyNodeRecords(NodeRecord* outRecords, size_t maxRecords) const;
  size_t copyRouteRecords(RouteRecord* outRecords, size_t maxRecords) const;
  bool resolveNodeIdByMac(const uint8_t* mac, uint32_t* outNodeId) const;
  uint32_t nodeId() const { return nodeId_; }

 private:
  struct RxQueueItem {
    uint8_t senderMac[6];
    int8_t rssi;
    uint16_t len;
    uint8_t data[kEspNowMaxPayload];
  };

  struct NeighborEntry {
    bool used = false;
    uint8_t mac[6]{};
    uint32_t nodeIdHint = 0;
    uint32_t lastSeenMs = 0;
    int16_t rssiEwmaQ8 = 0;
    uint16_t txOk = 0;
    uint16_t txFail = 0;
    uint16_t etxQ8 = 256;
  };

  struct RouteEntry {
    bool used = false;
    uint32_t dstNodeId = 0;
    uint8_t nextHopMac[6]{};
    uint32_t nextHopNodeId = 0;
    uint8_t hops = 0;
    uint16_t metricQ8 = 0;
    uint32_t learnedMs = 0;
  };

  static EspNowMesh* instance_;
  static void onSendStatic(const uint8_t* mac_addr, esp_now_send_status_t status);
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  static void onRecvStatic(const esp_now_recv_info_t* info, const uint8_t* data, int len);
#else
  static void onRecvStatic(const uint8_t* mac_addr, const uint8_t* data, int len);
#endif

  void onSend(const uint8_t* mac_addr, esp_now_send_status_t status);
  void onRecv(const uint8_t* mac_addr, int8_t rssi, const uint8_t* data, size_t len);

  bool enqueueRx(const uint8_t* mac_addr, int8_t rssi, const uint8_t* data, size_t len);
  void processRxQueue();
  void processFrame(const RxQueueItem& item);

  bool parseHeader(const uint8_t* data, size_t len, MeshFrameHeader* outHeader, const uint8_t** outBody,
                   size_t* outBodyLen) const;

  bool handleFragmentFrame(const MeshFrameHeader& header, const uint8_t* body, size_t bodyLen,
                           int8_t rssi, const uint8_t* senderMac, uint32_t nowMs);
  bool handleRoutedFragmentFrame(const MeshFrameHeader& header, const uint8_t* body, size_t bodyLen,
                                 int8_t rssi, const uint8_t* senderMac, uint32_t nowMs,
                                 RoutedFragmentMeta* outRouteMeta);
  bool handleNodeInfoFrame(const MeshFrameHeader& header, const uint8_t* body, size_t bodyLen,
                            int8_t rssi, uint32_t nowMs);

  bool sendPayload(AppPayloadType payloadType, const uint8_t* payload, size_t len, uint8_t ttl,
                   uint32_t* outMessageId);
  bool sendPayloadDirected(AppPayloadType payloadType, const uint8_t* payload, size_t len, uint32_t dstNodeId,
                           uint8_t ttl, uint32_t* outMessageId);
  bool sendNodeInfo(uint8_t ttl);
  bool sendRawBroadcast(const uint8_t* data, size_t len);
  bool sendRawUnicast(const uint8_t* mac, const uint8_t* data, size_t len);
  bool sendRawTo(const uint8_t* mac, const uint8_t* data, size_t len);
  uint8_t clampTtl(uint8_t ttl) const;
  uint8_t adaptiveAttemptBudget(uint8_t baseAttempts) const;
  bool ensurePeerForMac(const uint8_t* mac);
  void pruneRoutingTables(uint32_t nowMs);
  void learnRouteFromFrame(uint32_t originId, const uint8_t* senderMac, uint8_t hops, int8_t rssi,
                           uint32_t nowMs);
  bool selectRoute(uint32_t dstNodeId, RouteEntry* outRoute);
  bool lookupNodeMac(uint32_t nodeId, uint8_t* outMac) const;
  NeighborEntry* findNeighbor(const uint8_t* mac);
  NeighborEntry* upsertNeighbor(const uint8_t* mac);
  RouteEntry* findRoute(uint32_t dstNodeId);
  RouteEntry* upsertRoute(uint32_t dstNodeId);
  uint16_t computeRouteMetricQ8(const NeighborEntry* neighbor, uint8_t hops, int8_t rssi) const;
  static bool parseNodeIdString(const char* nodeIdText, uint32_t* outNodeId);
  bool queueInbound(const ReassembledMessage& message);

  uint32_t nextMessageId();
  bool upsertNode(uint32_t nodeId, NodeRecord** outNode);
  NodeRecord* findNode(uint32_t nodeId);

  QueueHandle_t rxQueue_ = nullptr;
  DuplicateFilter duplicateFilter_;
  DuplicateFilter parseRejectFilter_;
  ReassemblyManager reassembly_;
  MeshStats stats_{};

  NodeRecord nodes_[kMaxKnownNodes];
  uint8_t nodeCount_ = 0;
  NeighborEntry neighbors_[kMaxNeighborNodes]{};
  RouteEntry routes_[kMaxRouteEntries]{};

  ReassembledMessage inboundQueue_[kInboundMessageQueueDepth];
  uint8_t inboundHead_ = 0;
  uint8_t inboundTail_ = 0;
  uint8_t inboundCount_ = 0;

  uint32_t nodeId_ = 0;
  bool hasStaMac_ = false;
  uint8_t staMac_[6]{};
  uint32_t nextMessageId_ = 1;
  uint32_t nextNodeInfoDueMs_ = 0;
};

}  // namespace lpwa

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

  bool popReceivedMessage(ReassembledMessage* outMessage);

  void getStats(MeshStats* outStats) const;
  size_t copyNodeRecords(NodeRecord* outRecords, size_t maxRecords) const;
  uint32_t nodeId() const { return nodeId_; }

 private:
  struct RxQueueItem {
    uint8_t senderMac[6];
    int8_t rssi;
    uint16_t len;
    uint8_t data[kEspNowMaxPayload];
  };

  static EspNowMesh* instance_;
  static void onSendStatic(const uint8_t* mac_addr, esp_now_send_status_t status);
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  static void onRecvStatic(const esp_now_recv_info_t* info, const uint8_t* data, int len);
#else
  static void onRecvStatic(const uint8_t* mac_addr, const uint8_t* data, int len);
#endif

  void onSend(esp_now_send_status_t status);
  void onRecv(const uint8_t* mac_addr, int8_t rssi, const uint8_t* data, size_t len);

  bool enqueueRx(const uint8_t* mac_addr, int8_t rssi, const uint8_t* data, size_t len);
  void processRxQueue();
  void processFrame(const RxQueueItem& item);

  bool parseHeader(const uint8_t* data, size_t len, MeshFrameHeader* outHeader, const uint8_t** outBody,
                   size_t* outBodyLen) const;

  void handleFragmentFrame(const MeshFrameHeader& header, const uint8_t* body, size_t bodyLen,
                           uint32_t nowMs);
  void handleNodeInfoFrame(const MeshFrameHeader& header, const uint8_t* body, size_t bodyLen,
                           int8_t rssi, uint32_t nowMs);

  bool sendPayload(AppPayloadType payloadType, const uint8_t* payload, size_t len, uint8_t ttl,
                   uint32_t* outMessageId);
  bool sendNodeInfo(uint8_t ttl);
  bool sendRaw(const uint8_t* data, size_t len);
  bool queueInbound(const ReassembledMessage& message);

  uint32_t nextMessageId();
  bool upsertNode(uint32_t nodeId, NodeRecord** outNode);
  NodeRecord* findNode(uint32_t nodeId);

  QueueHandle_t rxQueue_ = nullptr;
  DuplicateFilter duplicateFilter_;
  ReassemblyManager reassembly_;
  MeshStats stats_{};

  NodeRecord nodes_[kMaxKnownNodes];
  uint8_t nodeCount_ = 0;

  ReassembledMessage inboundQueue_[kInboundMessageQueueDepth];
  uint8_t inboundHead_ = 0;
  uint8_t inboundTail_ = 0;
  uint8_t inboundCount_ = 0;

  uint32_t nodeId_ = 0;
  uint32_t nextMessageId_ = 1;
  uint32_t lastNodeInfoMs_ = 0;
};

}  // namespace lpwa


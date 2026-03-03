#pragma once

#include <Arduino.h>
#include <Stream.h>

#include "ble_relay.h"
#include "espnow_mesh.h"

namespace lpwa {

class SerialJsonBridge {
 public:
  SerialJsonBridge(EspNowMesh* mesh, BleRelay* ble);

  void begin(Stream* stream);
  void loop();

 private:
  static constexpr size_t kLineBufferSize = 4096;
  static constexpr uint8_t kMaxMeshDrainPerLoop = 24;
  static constexpr uint8_t kMaxBleDrainPerLoop = 24;

  struct BridgeStats {
    uint32_t commandCount = 0;
    uint32_t commandErrors = 0;
    uint32_t sentText = 0;
    uint32_t sentBinary = 0;
    uint32_t sentReliable = 0;
    uint32_t rxReliable = 0;
  };

  void handleLine(const char* line);
  void emitError(const char* code, const char* detail);
  void emitAck(const char* cmd, bool ok, const char* via, uint32_t messageId);
  void emitMeshMessage(const ReassembledMessage& message);
  void emitBleMessage(const BleRelayMessage& message);

  bool decodeBase64(const char* input, uint8_t* outBuffer, size_t outCapacity, size_t* outLen) const;
  String encodeBase64(const uint8_t* data, size_t len) const;

  static String formatNodeId(uint32_t nodeId);
  static String formatMac(const uint8_t* mac);

  EspNowMesh* mesh_;
  BleRelay* ble_;
  Stream* serial_ = nullptr;

  char lineBuffer_[kLineBufferSize]{};
  size_t lineLength_ = 0;
  bool droppingInputLine_ = false;
  BridgeStats bridgeStats_{};
  uint32_t lastTraceTelemetryMs_ = 0;
};

}  // namespace lpwa

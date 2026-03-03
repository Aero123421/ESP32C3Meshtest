#include <Arduino.h>

#include <inttypes.h>

#include "ble_relay.h"
#include "espnow_mesh.h"
#include "serial_json_bridge.h"

namespace {

uint32_t deriveNodeIdFromEfuse() {
  const uint64_t efuseMac = ESP.getEfuseMac();
  uint32_t nodeId = static_cast<uint32_t>((efuseMac >> 24) ^ (efuseMac & 0x00FFFFFFULL));
  if (nodeId == 0) {
    nodeId = 1;
  }
  return nodeId;
}

lpwa::EspNowMesh gMesh;
lpwa::BleRelay gBle;
lpwa::SerialJsonBridge gBridge(&gMesh, &gBle);

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(500);

#if LPWA_ENABLE_BLE_RELAY
  const bool bleReady = gBle.begin(deriveNodeIdFromEfuse());
#else
  const bool bleReady = false;
#endif
  const bool meshReady = gMesh.begin();
  gBridge.begin(&Serial);

  char nodeIdBuf[11];
  std::snprintf(nodeIdBuf, sizeof(nodeIdBuf), "0x%08" PRIX32, gMesh.nodeId());
  Serial.print("{\"event\":\"boot\",\"mesh_ready\":");
  Serial.print(meshReady ? "true" : "false");
  Serial.print(",\"ble_ready\":");
  Serial.print(bleReady ? "true" : "false");
  Serial.print(",\"mesh_channel\":");
  Serial.print(lpwa::kMeshChannel);
  Serial.print(",\"wifi_lr\":");
  Serial.print(lpwa::kWifiLongRangeDefault ? "true" : "false");
  Serial.print(",\"tx_power_qdbm\":");
  Serial.print(lpwa::kMeshTxPowerQuarterDbm);
  Serial.print(",\"node_id\":\"");
  Serial.print(nodeIdBuf);
  Serial.println("\"}");
}

void loop() {
  gMesh.loop();
#if LPWA_ENABLE_BLE_RELAY
  gBle.loop();
#endif
  gBridge.loop();
  delay(1);
}

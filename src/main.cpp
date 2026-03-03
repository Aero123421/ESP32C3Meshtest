#include <Arduino.h>

#include <inttypes.h>

#include "ble_relay.h"
#include "espnow_mesh.h"
#include "serial_json_bridge.h"

namespace {

lpwa::EspNowMesh gMesh;
lpwa::BleRelay gBle;
lpwa::SerialJsonBridge gBridge(&gMesh, &gBle);

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(500);

  const bool meshReady = gMesh.begin();
  const bool bleReady = gBle.begin(gMesh.nodeId());
  gBridge.begin(&Serial);

  char nodeIdBuf[11];
  std::snprintf(nodeIdBuf, sizeof(nodeIdBuf), "0x%08" PRIX32, gMesh.nodeId());
  Serial.print("{\"event\":\"boot\",\"mesh_ready\":");
  Serial.print(meshReady ? "true" : "false");
  Serial.print(",\"ble_ready\":");
  Serial.print(bleReady ? "true" : "false");
  Serial.print(",\"node_id\":\"");
  Serial.print(nodeIdBuf);
  Serial.println("\"}");
}

void loop() {
  gMesh.loop();
  gBle.loop();
  gBridge.loop();
  delay(1);
}


"""One-shot, exact-base source migration. Removed from the final PR tree."""
from pathlib import Path
import hashlib
BASE = {
 'src/espnow_mesh.cpp':'d2ee11fec09e33637b6bd0dfe41c8c7d38b627d0',
 'include/espnow_mesh.h':'1ba11157903e5c0d798bebed2eef5bd0559c2d15',
 'include/mesh_protocol.h':'747dae4ee6dc8bf515735480eefc50dfad6fe3eb',
 'src/serial_json_bridge.cpp':'a09b0ac02bef55d4ad35edbf0a57b9891eb78508',
 'src/main.cpp':'6152d1c57ec27e30bb34d2b8982bbe7311f5152f',
}
texts={}
for name, expected in BASE.items():
 b=Path(name).read_bytes()
 actual=hashlib.sha1(b'blob '+str(len(b)).encode()+b'\0'+b).hexdigest()
 assert actual==expected, (name, actual, expected)
 texts[name]=b.decode()
def sub(name, old, new, count=1):
 s=texts[name]
 assert s.count(old)==count, (name, old[:100], s.count(old), count)
 texts[name]=s.replace(old,new)
def function(name, start, following, replacement):
 s=texts[name]; assert s.count(start)==1 and s.count(following)==1
 a=s.index(start); b=s.index(following,a)
 texts[name]=s[:a]+replacement.rstrip()+'\n\n'+s[b:]
c='src/espnow_mesh.cpp'; h='include/espnow_mesh.h'; p='include/mesh_protocol.h'; b='src/serial_json_bridge.cpp'
sub(h,'#include <Arduino.h>','#include <Arduino.h>\n#include <atomic>')
sub(h,'  bool setRadioProfile(RadioProfile profile);','  bool setRadioProfile(RadioProfile profile);\n  void writeRadioStatus(Stream& output) const;')
sub(h,'  void processTxResultQueue();','  void processTxResultQueue();\n  void recordTxResult(const TxResultItem& item);')
sub(h,'    uint16_t etxQ8 = 256;','    uint16_t etxQ8 = 256;\n    uint8_t consecutiveTxFailures = 0;')
sub(h,'  QueueHandle_t rxQueue_ = nullptr;', '''  bool ready_ = false;
  bool txAwaiting_ = false;
  uint32_t txStartedMs_ = 0;
  uint32_t txCallbackTimeouts_ = 0;
  uint16_t radioRateKbps_ = 1000;
  std::atomic<uint32_t> callbackRxDrops_{0};
  std::atomic<uint32_t> callbackTxDrops_{0};
  QueueHandle_t rxQueue_ = nullptr;''')
sub(c,'#include "espnow_mesh.h"','#include "espnow_mesh.h"\n#include "mesh_validation.h"')
sub(c,'  instance_ = this;','  instance_ = this;\n  ready_ = false;\n  txAwaiting_ = false;')
sub(c,'  WiFi.mode(WIFI_STA);','  WiFi.persistent(false);\n  WiFi.setAutoReconnect(false);\n  if (!WiFi.mode(WIFI_STA)) return false;')
sub(c,'  (void)esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20);','  if (esp_wifi_set_country_code("JP", false) != ESP_OK) return false;\n  if (esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20) != ESP_OK) return false;')
sub(c,'  esp_now_register_send_cb(EspNowMesh::onSendStatic);\n  esp_now_register_recv_cb(EspNowMesh::onRecvStatic);','  if (esp_now_register_send_cb(EspNowMesh::onSendStatic) != ESP_OK ||\n      esp_now_register_recv_cb(EspNowMesh::onRecvStatic) != ESP_OK) return false;')
sub(c,'  nextNodeInfoDueMs_ = nowMs + firstDelay;\n  return true;','  nextNodeInfoDueMs_ = nowMs + firstDelay;\n  ready_ = true;\n  return true;')
sub(c,'void EspNowMesh::loop() {\n  processTxResultQueue();','''void EspNowMesh::loop() {
  if (!ready_) return;
  processTxResultQueue();
  // A missing callback must never be attributed to a later frame. Quarantine
  // sends and restart the radio/MCU only after a prolonged driver stall.
  if (txAwaiting_ && (millis() - txStartedMs_) > 10000U) {
    Serial.println("{\\"type\\":\\"error\\",\\"code\\":\\"tx_callback_stalled\\",\\"detail\\":\\"restarting radio\\"}");
    delay(20);
    ESP.restart();
    return;
  }
  pruneRoutingTables(millis());''')
sub(c,'  *outStats = stats_;','  *outStats = stats_;\n  outStats->rxQueueDropped += callbackRxDrops_.load();\n  outStats->txResultQueueDropped += callbackTxDrops_.load();')
sub(c,'  if (!applyRadioProfile(profile)) {\n    return false;\n  }','  if (txAwaiting_ || !ready_) return false;\n  if (!applyRadioProfile(profile)) {\n    (void)applyRadioProfile(radioProfile_);\n    return false;\n  }')
function(c,'bool EspNowMesh::applyRadioProfile(RadioProfile profile) {','void EspNowMesh::onSendStatic(', '')
sub(c,'  const bool success = (status == ESP_NOW_SEND_SUCCESS);\n  if (!enqueueTxResult(mac_addr, success)) {\n    stats_.txResultQueueDropped++;\n    if (success) {\n      stats_.txSuccess++;\n    } else {\n      stats_.txFailed++;\n    }\n  }', '  if (!enqueueTxResult(mac_addr, status == ESP_NOW_SEND_SUCCESS)) {\n    callbackTxDrops_.fetch_add(1);\n  }')
sub(c,'    stats_.rxQueueDropped++;\n  }\n}\n\nbool EspNowMesh::enqueueRx','    callbackRxDrops_.fetch_add(1);\n  }\n}\n\nbool EspNowMesh::enqueueRx')
function(c,'void EspNowMesh::processTxResultQueue() {','void EspNowMesh::processRxQueue()', '''void EspNowMesh::recordTxResult(const TxResultItem& item) {
  txAwaiting_ = false;
  if (item.success) ++stats_.txSuccess; else ++stats_.txFailed;
  if (!item.hasMac) return;  // broadcast TX completion is NOT receiver delivery
  NeighborEntry* neighbor = findNeighbor(item.mac);
  if (neighbor == nullptr) return;
  updateNeighborTxEtx(neighbor, item.success);
  neighbor->consecutiveTxFailures = item.success ? 0 :
      static_cast<uint8_t>(neighbor->consecutiveTxFailures < 255 ? neighbor->consecutiveTxFailures + 1 : 255);
  if (neighbor->consecutiveTxFailures < 2) return;
  const uint32_t now = millis();
  for (auto& route : routes_) {
    if (!route.used) continue;
    if (std::memcmp(route.backupNextHopMac, item.mac, 6) == 0) {
      std::memset(route.backupNextHopMac, 0, 6);
      route.backupLearnedMs = 0;
    }
    if (std::memcmp(route.nextHopMac, item.mac, 6) == 0) {
      std::memset(route.nextHopMac, 0, 6);
      if (isRouteLegValid(route.backupNextHopMac, route.backupLearnedMs, now)) {
        promoteBackup(&route, now);
      } else {
        route = RouteEntry{};
        ++stats_.routeExpired;
      }
    }
  }
}

void EspNowMesh::processTxResultQueue() {
  if (txResultQueue_ == nullptr) return;
  TxResultItem item{};
  while (xQueueReceive(txResultQueue_, &item, 0) == pdTRUE) recordTxResult(item);
}''')
sub(c,'  const uint32_t nowMs = millis();\n  pruneRoutingTables(nowMs);','''  if (header.originId == nodeId_) {
    ++stats_.droppedDuplicates;
    return;
  }
  if (!validMeshBody(header.type, body, bodyLen)) {
    ++stats_.rxParseErrors;
    return;
  }
  const uint32_t nowMs = millis();
  pruneRoutingTables(nowMs);''')
sub(c,'  *outBody = data + sizeof(MeshFrameHeader);','  if (!validMeshEnvelope(outHeader->originId, outHeader->ttl, outHeader->hops)) return false;\n  *outBody = data + sizeof(MeshFrameHeader);')
sub(c,'    if (selectRoute(routedMeta.dstNodeId, &route)) {','    if (selectRoute(routedMeta.dstNodeId, &route) &&\n        std::memcmp(route.nextHopMac, item.senderMac, 6) != 0) {')
sub(c,'      for (uint8_t attempt = 0; attempt < attempts; ++attempt) {\n        stats_.routedUnicastAttempts++;','''      for (uint8_t attempt = 0; attempt < attempts; ++attempt) {
        if (attempt > 0 && !selectRoute(routedMeta.dstNodeId, &route)) break;
        if (std::memcmp(route.nextHopMac, item.senderMac, 6) == 0) break;
        stats_.routedUnicastAttempts++;''')
sub(c,'  if (!forwarded && !(routedFrame && routedMeta.dstNodeId == nodeId_)) {','  if (!forwarded && !(routedFrame && routedMeta.dstNodeId == nodeId_)) {\n    delay(randomDelayMs(kForwardJitterMinMs, kForwardJitterMaxMs));')
function(c,'bool EspNowMesh::sendRawTo(const uint8_t* mac, const uint8_t* data, size_t len) {','uint8_t EspNowMesh::clampTtl(', '''bool EspNowMesh::sendRawTo(const uint8_t* mac, const uint8_t* data, size_t len) {
  if (!ready_ || mac == nullptr || data == nullptr || len == 0 || len > kEspNowMaxPayload) return false;
  processTxResultQueue();  // drain a late callback before allowing another TX
  if (txAwaiting_) return false;
  const uint8_t maxAttempts = static_cast<uint8_t>(kSendRawNoMemRetries + 1U);
  for (uint8_t attempt = 0; attempt < maxAttempts; ++attempt) {
    txAwaiting_ = true;
    txStartedMs_ = millis();
    const esp_err_t result = esp_now_send(mac, data, len);
    if (result == ESP_OK) {
      ++stats_.txFrames;
      TxResultItem item{};
      // Do NOT pump RX here: forwarding could recursively submit a second TX.
      // Only one frame is outstanding, so callback status identifies this frame.
      if (xQueueReceive(txResultQueue_, &item, pdMS_TO_TICKS(1000)) == pdTRUE) {
        recordTxResult(item);
        return item.success;
      }
      ++txCallbackTimeouts_;
      return false;  // remain quarantined until late callback or stall restart
    }
    txAwaiting_ = false;
    if (result != ESP_ERR_ESPNOW_NO_MEM || attempt + 1 >= maxAttempts) {
      if (result == ESP_ERR_ESPNOW_NO_MEM) ++stats_.txNoMemDrops;
      ++stats_.txFailed;
      return false;
    }
    ++stats_.txNoMemRetries;
    delay(randomDelayMs(kSendRawNoMemBackoffMinMs, kSendRawNoMemBackoffMaxMs));
  }
  return false;
}''')
sub(c,'  } else if (queued <= kAdaptiveQueueLowWater && attempts < kAdaptiveAttemptMax) {\n    attempts = static_cast<uint8_t>(attempts + 1);','')
sub(c,'      n = NeighborEntry{};','      (void)esp_now_del_peer(n.mac);\n      n = NeighborEntry{};')
sub(c,'  const int16_t sampleQ8 = static_cast<int16_t>(static_cast<int16_t>(rssi) << 8);','''  // Arduino 2 / IDF 4 callbacks have no RSSI: zero is unknown, NOT 0 dBm.
  if (rssi == 0) rssi = -85;
  const int16_t sampleQ8 = static_cast<int16_t>(static_cast<int16_t>(rssi) * 256);''')
sub(p,'#define LPWA_ENABLE_WIFI_LR 1','#define LPWA_ENABLE_WIFI_LR 0')
sub(p,'#define LPWA_ENABLE_BLE_RELAY 1','#define LPWA_ENABLE_BLE_RELAY 0')
sub(p,'constexpr uint8_t kDefaultTtl = 12;','constexpr uint8_t kDefaultTtl = 6;')
sub(p,'constexpr uint8_t kDefaultTtl = 10;','constexpr uint8_t kDefaultTtl = 6;')
sub(p,'constexpr uint32_t kNodeInfoPeriodMs = 10000;','constexpr uint32_t kNodeInfoPeriodMs = 15000;')
sub(p,'constexpr uint8_t kDefaultNodeInfoTtl = 5;','constexpr uint8_t kDefaultNodeInfoTtl = 3;')
sub(p,'constexpr uint8_t kOriginFrameRepeatCount = 4;','constexpr uint8_t kOriginFrameRepeatCount = 2;')
sub(p,'constexpr uint8_t kOriginFrameRepeatCount = 3;','constexpr uint8_t kOriginFrameRepeatCount = 2;')
sub(p,'constexpr uint8_t kForwardSendAttemptsFragment = 4;','constexpr uint8_t kForwardSendAttemptsFragment = 2;')
sub(p,'kMeshChannel <= 14, "LPWA_MESH_CHANNEL must be 1..14"','kMeshChannel <= 13, "LPWA_MESH_CHANNEL must be 1..13 (no ch14 in mixed PHY profiles)"')
sub(b,'''    DynamicJsonDocument out(256);
    out["event"] = "radio_profile";
    out["type"] = "radio_profile";
    out["profile"] = radioProfileName(mesh_->radioProfile());
    serializeJson(out, *serial_);
    serial_->println();''','''    mesh_->writeRadioStatus(*serial_);''')
sub(b,'        node["rssi"] = records[i].lastRssi;', '''        node["rssi_known"] = (records[i].lastRssi != 0);
        if (records[i].lastRssi != 0) node["rssi"] = records[i].lastRssi;
        else node["rssi"] = nullptr;
        node["age_ms"] = millis() - records[i].lastSeenMs;
        node["is_self"] = records[i].nodeId == mesh_->nodeId();''')
sub(b,'    DynamicJsonDocument out(2048);\n    out["event"] = "nodes";', '    DynamicJsonDocument out(12288);\n    out["event"] = "nodes";')
sub(b,'    DynamicJsonDocument out(4096);\n    out["event"] = "routes";', '    DynamicJsonDocument out(24576);\n    out["event"] = "routes";')
sub(b,'      out["count"] = exported;\n      out["total"] = count;','      out["count"] = exported;\n      out["total"] = count;\n      out["truncated"] = exported < count;')
sub('src/main.cpp','  Serial.print(lpwa::kWifiLongRangeDefault ? "true" : "false");','  Serial.print((meshReady && gMesh.radioProfile() == lpwa::EspNowMesh::RadioProfile::LongRange) ? "true" : "false");')
sub('src/main.cpp','  Serial.println("\\\"}");','  Serial.println("\\\"}");\n  gMesh.writeRadioStatus(Serial);')
for name, content in texts.items():
 Path(name).write_text(content, encoding='utf-8')
 print('updated',name)

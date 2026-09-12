#include "espnow_mesh.h"
#include <ArduinoJson.h>
#include <esp_wifi.h>
#include <esp_idf_version.h>
#include <esp_system.h>
#include <inttypes.h>

namespace lpwa {
bool EspNowMesh::applyRadioProfile(RadioProfile profile) {
  uint8_t mask = WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N;
  wifi_phy_rate_t rate = WIFI_PHY_RATE_1M_L;
  uint16_t kbps = 1000;
  wifi_ps_type_t ps = (kBleRelayDefault || profile == RadioProfile::Coexist)
      ? WIFI_PS_MIN_MODEM : WIFI_PS_NONE;
  if (profile == RadioProfile::LongRange) {
#if LPWA_ENABLE_WIFI_LR && (!LPWA_ENABLE_BLE_RELAY || LPWA_ALLOW_WIFI_LR_WITH_BLE)
    mask |= WIFI_PROTOCOL_LR;
    rate = WIFI_PHY_RATE_LORA_250K;
    kbps = 250;
#else
    return false;
#endif
  }
  // Protocol capability alone does not select the ESP-NOW transmit PHY.
  // This repository pins IDF 4.4 / Arduino 2.0.17; migrate this call when
  // deliberately upgrading the toolchain (IDF 6 removes the legacy API).
  if (esp_wifi_set_ps(ps) != ESP_OK ||
      esp_wifi_set_protocol(WIFI_IF_STA, mask) != ESP_OK ||
      esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20) != ESP_OK ||
      esp_wifi_config_espnow_rate(WIFI_IF_STA, rate) != ESP_OK ||
      esp_wifi_set_max_tx_power(kMeshTxPowerQuarterDbm) != ESP_OK) return false;

  uint8_t actualMask = 0, channel = 0;
  wifi_second_chan_t secondary{};
  wifi_bandwidth_t bandwidth{};
  wifi_ps_type_t actualPs{};
  int8_t actualPower = 0;
  if (esp_wifi_get_protocol(WIFI_IF_STA, &actualMask) != ESP_OK ||
      esp_wifi_get_bandwidth(WIFI_IF_STA, &bandwidth) != ESP_OK ||
      esp_wifi_get_channel(&channel, &secondary) != ESP_OK ||
      esp_wifi_get_ps(&actualPs) != ESP_OK ||
      esp_wifi_get_max_tx_power(&actualPower) != ESP_OK) return false;
  if (actualMask != mask || bandwidth != WIFI_BW_HT20 || channel != kMeshChannel ||
      actualPs != ps || actualPower > kMeshTxPowerQuarterDbm) return false;
  radioRateKbps_ = kbps;
  return true;
}

void EspNowMesh::writeRadioStatus(Stream& output) const {
  DynamicJsonDocument out(1024);
  out["event"] = "radio_profile";
  out["type"] = "radio_profile";
  out["ready"] = ready_;
  out["chip"] = ESP.getChipModel();
  out["sdk"] = esp_get_idf_version();
  out["wire_version"] = kMeshVersion;
  char id[11];
  snprintf(id, sizeof(id), "0x%08" PRIX32, nodeId_);
  out["node_id"] = id;
  out["profile"] = radioProfile_ == RadioProfile::LongRange ? "long_range" :
      (radioProfile_ == RadioProfile::Coexist ? "coexist" : "balanced");
  out["ble_enabled"] = kBleRelayDefault;
  out["tx_power_requested_qdbm"] = kMeshTxPowerQuarterDbm;
  out["tx_callback_timeouts"] = txCallbackTimeouts_;
  out["tx_waiting"] = txAwaiting_;
  out["uptime_ms"] = millis();
  // Rate has no getter in the pinned SDK: report API-accepted configuration,
  // explicitly NOT a measured on-air rate or a received-signal measurement.
  out["espnow_rate_kbps"] = ready_ ? radioRateKbps_ : 0;
  out["rate_source"] = "configured_api";
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  out["rssi_source"] = "rx_ctrl_last_hop";
#else
  out["rssi_source"] = "unavailable_in_idf4_callback";
#endif
  uint8_t mask = 0, channel = 0;
  wifi_second_chan_t secondary{};
  wifi_bandwidth_t bandwidth{};
  wifi_ps_type_t ps{};
  wifi_country_t country{};
  int8_t power = 0;
  bool readback = esp_wifi_get_protocol(WIFI_IF_STA, &mask) == ESP_OK &&
      esp_wifi_get_bandwidth(WIFI_IF_STA, &bandwidth) == ESP_OK &&
      esp_wifi_get_channel(&channel, &secondary) == ESP_OK &&
      esp_wifi_get_ps(&ps) == ESP_OK && esp_wifi_get_country(&country) == ESP_OK &&
      esp_wifi_get_max_tx_power(&power) == ESP_OK;
  out["readback_ok"] = readback;
  if (readback) {
    out["channel"] = channel;
    out["bandwidth_mhz"] = bandwidth == WIFI_BW_HT20 ? 20 : 40;
    out["protocol_mask"] = mask;
    out["lr_enabled"] = (mask & WIFI_PROTOCOL_LR) != 0;
    out["power_save"] = ps != WIFI_PS_NONE;
    out["tx_power_readback_qdbm"] = power;
    char cc[3] = {country.cc[0], country.cc[1], 0};
    out["country"] = cc;
  }
  serializeJson(out, output);
  output.println();
}
}  // namespace lpwa

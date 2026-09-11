"""Compile actual firmware validation/TX code against a deterministic fake driver."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_firmware_geometry_and_actual_send_callback_path(tmp_path):
    compiler = shutil.which('g++') or shutil.which('clang++')
    if not compiler or sys.platform == 'win32':
        pytest.skip('Native C++ harness runs on Linux and macOS; six embedded targets build separately')
    (tmp_path / 'Arduino.h').write_text('#pragma once\n#include <cstdint>\n#include <cstddef>\n')
    source = (ROOT / 'src/espnow_mesh.cpp').read_text(encoding='utf-8')
    start = source.index('bool EspNowMesh::sendRawTo(')
    end = source.index('uint8_t EspNowMesh::clampTtl(', start)
    actual_send = source[start:end]
    harness = r'''
#include <cassert>
#include <deque>
#include <cstring>
#include "mesh_validation.h"
using namespace lpwa;
using esp_err_t = int;
constexpr int ESP_OK = 0, ESP_ERR_ESPNOW_NO_MEM = 1, pdTRUE = 1;
constexpr int ESP_FAIL = 2;
uint32_t timeNow=0;
uint32_t millis(){return timeNow;}
void delay(int ms){timeNow+=ms;}
int pdMS_TO_TICKS(int ms){return ms;}
int randomDelayMs(int low,int){return low;}
struct TxResultItem {bool hasMac=false; uint8_t mac[6]{}; bool success=false;};
std::deque<TxResultItem> results;
int calls=0, failuresBeforeAccept=0; bool deliverCallback=true, callbackSuccess=true;
int esp_now_send(const uint8_t*, const uint8_t*, size_t){
  ++calls;
  if(failuresBeforeAccept-- > 0) return ESP_ERR_ESPNOW_NO_MEM;
  if(deliverCallback) results.push_back(TxResultItem{false,{},callbackSuccess});
  return ESP_OK;
}
int xQueueReceive(void*,TxResultItem* out,int){
  if(results.empty())return 0;
  *out=results.front();results.pop_front();return pdTRUE;
}
class EspNowMesh {
public:
  bool ready_=true,txAwaiting_=false;
  uint32_t txStartedMs_=0,txCallbackTimeouts_=0;
  void* txResultQueue_=nullptr;
  struct Stats {int txFrames=0,txNoMemDrops=0,txFailed=0,txNoMemRetries=0,txSuccess=0;} stats_;
  void recordTxResult(const TxResultItem& i){txAwaiting_=false;if(i.success)stats_.txSuccess++;else stats_.txFailed++;}
  void processTxResultQueue(){while(!results.empty()){auto i=results.front();results.pop_front();recordTxResult(i);}}
  bool sendRawTo(const uint8_t*,const uint8_t*,size_t);
};
'''
    assertions = r'''
int main(){
  assert(validMeshEnvelope(1,6,0));
  assert(validMeshEnvelope(1,1,13));
  assert(!validMeshEnvelope(0,6,0));
  assert(!validMeshEnvelope(1,0,0));
  assert(!validMeshEnvelope(1,14,1));
  assert(!validMeshEnvelope(1,1,255));
  FragmentMeta m{};m.appType=1;m.fragCount=1;m.totalLen=1;m.chunkLen=1;
  assert(validFragmentGeometry(m,1));
  m.fragCount=2;assert(!validFragmentGeometry(m,1));
  m.fragCount=1;m.fragIndex=1;assert(!validFragmentGeometry(m,1));
  m.fragIndex=0;m.chunkLen=0;assert(!validFragmentGeometry(m,0));
  m.totalLen=1024;m.fragCount=6;m.fragIndex=5;m.chunkLen=124;
  assert(validFragmentGeometry(m,124));
  m.chunkLen=123;assert(!validFragmentGeometry(m,123));
  m.totalLen=1025;assert(!validFragmentGeometry(m,123));
  uint8_t frame[250]{};
  assert(!validMeshBody(99,frame,8));
  assert(!validMeshBody(3,frame,12));
  assert(!validMeshBody(1,nullptr,0));
  assert(validMeshBody(2,frame,28));
  assert(!validMeshBody(2,frame,27));

  uint8_t mac[6]={1,2,3,4,5,6}, data[1]={42};
  EspNowMesh mesh;
  callbackSuccess=false;
  assert(!mesh.sendRawTo(mac,data,1));
  assert(mesh.stats_.txFrames==1 && mesh.stats_.txFailed==1 && !mesh.txAwaiting_);
  callbackSuccess=true;
  assert(mesh.sendRawTo(mac,data,1));
  assert(mesh.stats_.txSuccess==1);
  deliverCallback=false;
  assert(!mesh.sendRawTo(mac,data,1));
  assert(mesh.txAwaiting_ && mesh.txCallbackTimeouts_==1);
  int before=calls;
  assert(!mesh.sendRawTo(mac,data,1) && calls==before);
  results.push_back(TxResultItem{false,{},true});
  deliverCallback=true;
  assert(mesh.sendRawTo(mac,data,1));
  failuresBeforeAccept=10;
  before=calls;
  assert(!mesh.sendRawTo(mac,data,1));
  assert(calls-before==kSendRawNoMemRetries+1);
  assert(!mesh.txAwaiting_ && mesh.stats_.txNoMemDrops==1);
  mesh.ready_=false;
  before=calls;assert(!mesh.sendRawTo(mac,data,1));assert(calls==before);
  return 0;
}
'''
    cpp = tmp_path / 'mesh_test.cpp'
    cpp.write_text(harness + '\n' + actual_send + assertions, encoding='utf-8')
    binary = tmp_path / 'mesh_test'
    result = subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-fsanitize=undefined',
                             '-I', str(tmp_path), '-I', str(ROOT / 'include'), str(cpp), '-o', str(binary)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr

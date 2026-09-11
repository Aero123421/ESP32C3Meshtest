#pragma once
#include <cstdint>
#include <cstddef>
#include <cstring>
#include "mesh_protocol.h"

namespace lpwa {
inline bool validMeshEnvelope(uint32_t origin, uint8_t ttl, uint8_t hops) {
  return origin != 0 && ttl != 0 && ttl <= kMaxTtl && hops < kMaxTtl &&
      static_cast<unsigned>(ttl) + hops <= kMaxTtl;
}
inline bool validFragmentGeometry(const FragmentMeta& m, size_t bytes) {
  if (m.appType != static_cast<uint8_t>(AppPayloadType::Text) &&
      m.appType != static_cast<uint8_t>(AppPayloadType::Binary)) return false;
  if (m.totalLen > kMaxAppPayload || m.chunkLen > kFragmentChunkSize || m.chunkLen != bytes) return false;
  const size_t count = m.totalLen == 0 ? 1 : (m.totalLen + kFragmentChunkSize - 1) / kFragmentChunkSize;
  if (m.fragCount != count || m.fragIndex >= count) return false;
  const size_t offset = m.fragIndex * kFragmentChunkSize;
  const size_t remaining = m.totalLen - offset;
  return m.chunkLen == (remaining > kFragmentChunkSize ? kFragmentChunkSize : remaining);
}
inline bool validMeshBody(uint8_t kind, const uint8_t* body, size_t len) {
  if (body == nullptr) return false;
  if (kind == static_cast<uint8_t>(FrameType::NodeInfo)) {
    // Legacy NodeInfo did not carry the 6-byte MAC suffix.
    return len == sizeof(NodeInfoPayload) || len == 22;
  }
  size_t prefix = 0;
  if (kind == static_cast<uint8_t>(FrameType::RoutedFragment)) {
    prefix = sizeof(RoutedFragmentMeta);
    if (len < prefix) return false;
    RoutedFragmentMeta route{};
    std::memcpy(&route, body, prefix);
    if (route.dstNodeId == 0) return false;
  } else if (kind != static_cast<uint8_t>(FrameType::Fragment)) return false;
  if (len < prefix + sizeof(FragmentMeta)) return false;
  FragmentMeta m{};
  std::memcpy(&m, body + prefix, sizeof(m));
  return validFragmentGeometry(m, len - prefix - sizeof(m));
}
}  // namespace lpwa

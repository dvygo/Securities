#include "premarket/zipread.hpp"

#include <zlib.h>

#include <cstdint>
#include <cstring>

#include "premarket/util.hpp"

namespace premarket::zipread {

namespace {

struct Entry {
    std::string name;
    std::uint16_t method = 0;
    std::uint32_t compressed = 0;
    std::uint32_t size = 0;
    std::uint32_t crc = 0;
    std::uint32_t offset = 0;
};

std::uint16_t le16(const std::string& b, std::size_t at) {
    return static_cast<std::uint16_t>(static_cast<unsigned char>(b[at]) | static_cast<unsigned char>(b[at + 1]) << 8);
}

std::uint32_t le32(const std::string& b, std::size_t at) {
    return static_cast<std::uint32_t>(le16(b, at)) | static_cast<std::uint32_t>(le16(b, at + 2)) << 16;
}

std::vector<Entry> directory(const std::string& data, const std::string& label) {
    if (data.size() < 22) throw ValueError("File is not a zip file: " + label);
    std::size_t eocd = std::string::npos;
    for (std::size_t i = data.size() - 22 + 1; i-- > 0 && data.size() - i <= 22 + 65535;)
        if (le32(data, i) == 0x06054b50) {
            eocd = i;
            break;
        }
    if (eocd == std::string::npos) throw ValueError("File is not a zip file: " + label);
    std::uint16_t count = le16(data, eocd + 10);
    std::uint32_t cd = le32(data, eocd + 16);
    if (cd == 0xFFFFFFFF || count == 0xFFFF) throw ValueError(label + ": zip64 archives are not supported");
    std::vector<Entry> out;
    std::size_t p = cd;
    for (std::uint16_t i = 0; i < count; ++i) {
        if (p + 46 > data.size() || le32(data, p) != 0x02014b50) throw ValueError(label + ": bad zip central directory");
        Entry e;
        e.method = le16(data, p + 10);
        e.crc = le32(data, p + 16);
        e.compressed = le32(data, p + 20);
        e.size = le32(data, p + 24);
        std::uint16_t n = le16(data, p + 28), extra = le16(data, p + 30), comment = le16(data, p + 32);
        e.offset = le32(data, p + 42);
        e.name = data.substr(p + 46, n);
        out.push_back(e);
        p += 46 + n + extra + comment;
    }
    return out;
}

}  // namespace

std::vector<std::string> names(const std::filesystem::path& zip) {
    std::vector<std::string> out;
    for (const auto& e : directory(util::read_file(zip), zip.filename().string())) out.push_back(e.name);
    return out;
}

std::string read(const std::filesystem::path& zip, const std::string& name) {
    std::string data = util::read_file(zip);
    for (const auto& e : directory(data, zip.filename().string())) {
        if (e.name != name) continue;
        std::size_t p = e.offset;
        if (p + 30 > data.size() || le32(data, p) != 0x04034b50) throw ValueError(name + ": bad zip local header");
        std::size_t start = p + 30 + le16(data, p + 26) + le16(data, p + 28);
        if (start + e.compressed > data.size()) throw ValueError(name + ": truncated zip entry");
        std::string out;
        if (e.method == 0) {
            out = data.substr(start, e.compressed);
        } else if (e.method == 8) {
            out.resize(e.size);
            z_stream z{};
            inflateInit2(&z, -15);
            z.next_in = reinterpret_cast<Bytef*>(data.data() + start);
            z.avail_in = e.compressed;
            z.next_out = reinterpret_cast<Bytef*>(out.data());
            z.avail_out = e.size;
            int rc = inflate(&z, Z_FINISH);
            inflateEnd(&z);
            if (rc != Z_STREAM_END || z.total_out != e.size) throw ValueError(name + ": cannot inflate zip entry");
        } else {
            throw ValueError(name + ": zip compression method " + std::to_string(e.method) + " is not supported");
        }
        if (crc32(0L, reinterpret_cast<const Bytef*>(out.data()), static_cast<uInt>(out.size())) != e.crc)
            throw ValueError(name + ": zip CRC mismatch");
        return out;
    }
    throw ValueError("There is no item named " + util::quoted(name) + " in the archive");
}

}  // namespace premarket::zipread

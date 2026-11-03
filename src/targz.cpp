#include "premarket/targz.hpp"

#include <grp.h>
#include <pwd.h>
#include <sys/stat.h>
#include <zlib.h>

#include <array>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <vector>

#include "premarket/util.hpp"

namespace premarket::targz {

namespace fs = std::filesystem;

struct Writer::Impl {
    gzFile gz = nullptr;
    bool closed = false;

    void put(const void* data, std::size_t n) {
        if (n == 0) return;
        if (gzwrite(gz, data, static_cast<unsigned>(n)) != static_cast<int>(n))
            throw RuntimeError("tar.gz: write failed");
    }
};

namespace {

void octal(char* field, std::size_t width, unsigned long long value) {
    std::snprintf(field, width, "%0*llo", static_cast<int>(width - 1), value);
}

std::string owner_name(uid_t uid) {
    if (auto* pw = ::getpwuid(uid); pw && pw->pw_name) return pw->pw_name;
    return "";
}

std::string group_name(gid_t gid) {
    if (auto* gr = ::getgrgid(gid); gr && gr->gr_name) return gr->gr_name;
    return "";
}

std::array<char, 512> header(const std::string& name, const struct stat& st, char type, unsigned long long size) {
    std::array<char, 512> h{};
    std::string path = name;
    std::string prefix;
    if (path.size() > 100) {
        bool split = false;
        for (auto p = path.find('/'); p != std::string::npos; p = path.find('/', p + 1)) {
            if (p <= 155 && path.size() - p - 1 <= 100) {
                prefix = path.substr(0, p);
                path = path.substr(p + 1);
                split = true;
                break;
            }
        }
        if (!split) throw RuntimeError("tar.gz: name too long: " + name);
    }
    std::memcpy(h.data(), path.data(), path.size());
    octal(h.data() + 100, 8, st.st_mode & 07777);
    octal(h.data() + 108, 8, st.st_uid);
    octal(h.data() + 116, 8, st.st_gid);
    octal(h.data() + 124, 12, size);
    octal(h.data() + 136, 12, static_cast<unsigned long long>(st.st_mtime));
    std::memset(h.data() + 148, ' ', 8);
    h[156] = type;
    std::memcpy(h.data() + 257, "ustar", 6);
    std::memcpy(h.data() + 263, "00", 2);
    auto uname = owner_name(st.st_uid).substr(0, 31);
    auto gname = group_name(st.st_gid).substr(0, 31);
    std::memcpy(h.data() + 265, uname.data(), uname.size());
    std::memcpy(h.data() + 297, gname.data(), gname.size());
    std::memcpy(h.data() + 345, prefix.data(), prefix.size());
    unsigned sum = 0;
    for (unsigned char c : h) sum += c;
    std::snprintf(h.data() + 148, 8, "%06o", sum);
    h[155] = ' ';
    return h;
}

}  // namespace

Writer::Writer(const fs::path& path) : impl_(std::make_unique<Impl>()) {
    impl_->gz = gzopen(path.c_str(), "wb9");
    if (!impl_->gz) throw RuntimeError("tar.gz: cannot open " + path.string());
}

Writer::~Writer() {
    if (impl_ && !impl_->closed && impl_->gz) gzclose(impl_->gz);
}

void Writer::add(const fs::path& source, const std::string& arcname) { add_entry(source, arcname); }

void Writer::add_entry(const fs::path& source, const std::string& arcname) {
    struct stat st{};
    if (::lstat(source.c_str(), &st) != 0) throw RuntimeError("tar.gz: cannot stat " + source.string());
    if (S_ISDIR(st.st_mode)) {
        auto h = header(arcname, st, '5', 0);
        impl_->put(h.data(), h.size());
        for (const auto& child : util::list_dir_sorted(source))
            add_entry(child, arcname + "/" + child.filename().string());
        return;
    }
    if (!S_ISREG(st.st_mode)) return;
    auto h = header(arcname, st, '0', static_cast<unsigned long long>(st.st_size));
    impl_->put(h.data(), h.size());
    std::ifstream in(source, std::ios::binary);
    std::vector<char> buf(1 << 16);
    unsigned long long written = 0;
    while (in) {
        in.read(buf.data(), static_cast<std::streamsize>(buf.size()));
        auto got = static_cast<std::size_t>(in.gcount());
        impl_->put(buf.data(), got);
        written += got;
    }
    if (written != static_cast<unsigned long long>(st.st_size)) throw RuntimeError("tar.gz: " + source.string() + " changed while archiving");
    std::size_t pad = (512 - written % 512) % 512;
    std::array<char, 512> zeros{};
    impl_->put(zeros.data(), pad);
}

void Writer::close() {
    if (impl_->closed) return;
    std::array<char, 1024> end{};
    impl_->put(end.data(), end.size());
    if (gzclose(impl_->gz) != Z_OK) throw RuntimeError("tar.gz: close failed");
    impl_->closed = true;
}

}  // namespace premarket::targz

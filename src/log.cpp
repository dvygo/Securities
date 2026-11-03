#include "premarket/log.hpp"

#include <cstdio>
#include <fstream>
#include <mutex>
#include <optional>

namespace premarket::log {

namespace {

std::mutex mu;
std::optional<std::filesystem::path> tee_path;

}  // namespace

void out(const std::string& line) {
    std::lock_guard<std::mutex> lock(mu);
    std::fwrite(line.data(), 1, line.size(), stdout);
    std::fputc('\n', stdout);
    std::fflush(stdout);
}

void err_raw(const std::string& text) {
    std::lock_guard<std::mutex> lock(mu);
    std::fwrite(text.data(), 1, text.size(), stderr);
    std::fflush(stderr);
    if (tee_path) {
        std::ofstream f(*tee_path, std::ios::app | std::ios::binary);
        f.write(text.data(), static_cast<std::streamsize>(text.size()));
    }
}

void err(const std::string& line) { err_raw(line + "\n"); }

void flush() {
    std::fflush(stdout);
    std::fflush(stderr);
}

Tee::Tee(const std::filesystem::path& path) {
    std::lock_guard<std::mutex> lock(mu);
    std::ofstream(path, std::ios::trunc | std::ios::binary);
    tee_path = path;
}

Tee::~Tee() {
    std::lock_guard<std::mutex> lock(mu);
    tee_path.reset();
    std::fflush(stderr);
}

}  // namespace premarket::log

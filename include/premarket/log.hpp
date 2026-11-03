#pragma once

#include <filesystem>
#include <string>

namespace premarket::log {

void out(const std::string& line);
void err(const std::string& line);
void err_raw(const std::string& text);
void flush();

class Tee {
public:
    explicit Tee(const std::filesystem::path& path);
    ~Tee();
    Tee(const Tee&) = delete;
    Tee& operator=(const Tee&) = delete;
};

}  // namespace premarket::log

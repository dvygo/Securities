#pragma once

#include <filesystem>
#include <memory>
#include <string>

namespace premarket::targz {

class Writer {
public:
    explicit Writer(const std::filesystem::path& path);
    ~Writer();
    Writer(const Writer&) = delete;
    Writer& operator=(const Writer&) = delete;

    void add(const std::filesystem::path& source, const std::string& arcname);
    void close();

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
    void add_entry(const std::filesystem::path& source, const std::string& arcname);
};

}  // namespace premarket::targz

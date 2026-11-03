#pragma once

#include <cstdint>
#include <filesystem>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <vector>

namespace premarket::ini {

struct Entry {
    std::string value;
    int line = 0;
};

struct Section {
    std::string name;
    int line = 0;
    std::map<std::string, Entry> entries;
    std::vector<std::string> order;
};

class File {
public:
    static File read(const std::filesystem::path& path);
    static File parse(const std::string& text, const std::string& source);

    const std::string& source() const { return source_; }
    const Section* find(const std::string& section) const;
    const Section& at(const std::string& section) const;
    std::vector<const Section*> sections() const;
    std::string where(const Section& section, const std::string& key = "") const;

    std::string text(const Section& s, const std::string& key, const std::string& fallback = "") const;
    std::string required(const Section& s, const std::string& key) const;
    std::int64_t integer(const Section& s, const std::string& key, std::optional<std::int64_t> fallback = {}) const;
    bool flag(const Section& s, const std::string& key, std::optional<bool> fallback = {}) const;
    std::filesystem::path absolute_path(const Section& s, const std::string& key, bool required = true) const;
    void only(const Section& s, const std::set<std::string>& allowed) const;

private:
    std::string source_;
    std::vector<Section> sections_;
};

}  // namespace premarket::ini

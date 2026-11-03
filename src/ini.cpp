#include "premarket/ini.hpp"

#include "premarket/util.hpp"

namespace premarket::ini {

namespace {

bool key_char(char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-' ||
           c == '.';
}

std::string strip_inline_comment(const std::string& raw) {
    if (!raw.empty() && (raw[0] == '#' || raw[0] == ';')) return "";
    for (std::size_t i = 1; i < raw.size(); ++i)
        if ((raw[i] == '#' || raw[i] == ';') && (raw[i - 1] == ' ' || raw[i - 1] == '\t')) return util::rstrip(raw.substr(0, i));
    return raw;
}

}  // namespace

File File::read(const std::filesystem::path& path) {
    std::error_code ec;
    if (!std::filesystem::is_regular_file(path, ec)) throw FileNotFoundError(path.string() + ": no such file");
    return parse(util::read_file(path), path.string());
}

File File::parse(const std::string& text, const std::string& source) {
    File file;
    file.source_ = source;
    Section* current = nullptr;
    auto fail = [&](int line, const std::string& msg) {
        throw ValueError(source + ":" + std::to_string(line) + ": " + msg);
    };
    std::string body = text;
    if (util::starts_with(body, "\xEF\xBB\xBF")) body.erase(0, 3);
    int lineno = 0;
    for (const auto& raw : util::split(body, "\n")) {
        ++lineno;
        std::string line = util::strip(raw);
        if (line.empty() || line[0] == '#' || line[0] == ';') continue;
        if (line[0] == '[') {
            auto close = line.find(']');
            if (close == std::string::npos) fail(lineno, "section header without ']'");
            std::string rest = util::strip(line.substr(close + 1));
            if (!rest.empty() && rest[0] != '#' && rest[0] != ';') fail(lineno, "text after the section header");
            std::string name = util::strip(line.substr(1, close - 1));
            if (name.empty()) fail(lineno, "empty section name");
            for (const auto& s : file.sections_)
                if (s.name == name)
                    fail(lineno, "section [" + name + "] repeated (first at line " + std::to_string(s.line) + ")");
            file.sections_.push_back(Section{name, lineno, {}, {}});
            current = &file.sections_.back();
            continue;
        }
        auto eq = line.find('=');
        if (eq == std::string::npos) fail(lineno, "expected `key = value`");
        if (current == nullptr) fail(lineno, "`" + util::strip(line.substr(0, eq)) + "` is outside any [section]");
        std::string key = util::strip(line.substr(0, eq));
        if (key.empty()) fail(lineno, "empty key");
        for (char c : key)
            if (!key_char(c)) fail(lineno, "key `" + key + "` may only use letters, digits, '_', '-' and '.'");
        std::string value = util::strip(line.substr(eq + 1));
        if (!value.empty() && value[0] == '"') {
            std::string out;
            std::size_t i = 1;
            bool closed = false;
            for (; i < value.size(); ++i) {
                char c = value[i];
                if (c == '\\' && i + 1 < value.size() && (value[i + 1] == '"' || value[i + 1] == '\\')) {
                    out.push_back(value[++i]);
                } else if (c == '"') {
                    closed = true;
                    break;
                } else {
                    out.push_back(c);
                }
            }
            if (!closed) fail(lineno, "unterminated quoted value for `" + key + "`");
            std::string rest = util::strip(value.substr(i + 1));
            if (!rest.empty() && rest[0] != '#' && rest[0] != ';') fail(lineno, "text after the quoted value of `" + key + "`");
            value = out;
        } else {
            value = strip_inline_comment(value);
        }
        if (auto it = current->entries.find(key); it != current->entries.end())
            fail(lineno, "`" + key + "` repeated in [" + current->name + "] (first at line " +
                             std::to_string(it->second.line) + ")");
        current->entries[key] = Entry{value, lineno};
        current->order.push_back(key);
    }
    return file;
}

const Section* File::find(const std::string& section) const {
    for (const auto& s : sections_)
        if (s.name == section) return &s;
    return nullptr;
}

const Section& File::at(const std::string& section) const {
    if (const auto* s = find(section)) return *s;
    throw ValueError(source_ + ": missing section [" + section + "]");
}

std::vector<const Section*> File::sections() const {
    std::vector<const Section*> out;
    for (const auto& s : sections_) out.push_back(&s);
    return out;
}

std::string File::where(const Section& s, const std::string& key) const {
    if (!key.empty())
        if (auto it = s.entries.find(key); it != s.entries.end())
            return source_ + ":" + std::to_string(it->second.line) + ": [" + s.name + "] " + key;
    return source_ + ":" + std::to_string(s.line) + ": [" + s.name + "]" + (key.empty() ? "" : " " + key);
}

std::string File::text(const Section& s, const std::string& key, const std::string& fallback) const {
    auto it = s.entries.find(key);
    return it == s.entries.end() ? fallback : it->second.value;
}

std::string File::required(const Section& s, const std::string& key) const {
    auto it = s.entries.find(key);
    if (it == s.entries.end() || it->second.value.empty()) throw ValueError(where(s, key) + " is required");
    return it->second.value;
}

std::int64_t File::integer(const Section& s, const std::string& key, std::optional<std::int64_t> fallback) const {
    auto it = s.entries.find(key);
    if (it == s.entries.end()) {
        if (fallback) return *fallback;
        throw ValueError(where(s, key) + " is required");
    }
    const std::string& v = it->second.value;
    if (v.empty() || !util::is_digits(v[0] == '-' ? v.substr(1) : v))
        throw ValueError(where(s, key) + " must be a whole number, got `" + v + "`");
    return std::stoll(v);
}

bool File::flag(const Section& s, const std::string& key, std::optional<bool> fallback) const {
    auto it = s.entries.find(key);
    if (it == s.entries.end()) {
        if (fallback) return *fallback;
        throw ValueError(where(s, key) + " is required");
    }
    if (it->second.value == "1") return true;
    if (it->second.value == "0") return false;
    throw ValueError(where(s, key) + " must be 0 or 1, got `" + it->second.value + "`");
}

std::filesystem::path File::absolute_path(const Section& s, const std::string& key, bool required_) const {
    auto it = s.entries.find(key);
    if (it == s.entries.end() || it->second.value.empty()) {
        if (required_) throw ValueError(where(s, key) + " is required (an absolute path)");
        return {};
    }
    std::filesystem::path p(it->second.value);
    if (!p.is_absolute()) throw ValueError(where(s, key) + " must be an absolute path, got `" + it->second.value + "`");
    return p.lexically_normal();
}

void File::only(const Section& s, const std::set<std::string>& allowed) const {
    for (const auto& key : s.order)
        if (!allowed.count(key)) {
            std::vector<std::string> names(allowed.begin(), allowed.end());
            throw ValueError(where(s, key) + " is not a known key (known: " + util::join(names, ", ") + ")");
        }
}

}  // namespace premarket::ini

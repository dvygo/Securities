#include "premarket/csv.hpp"

namespace premarket::csv {

std::vector<std::string> parse_line(std::string_view line) {
    if (!line.empty() && line.back() == '\r') line.remove_suffix(1);
    std::vector<std::string> fields;
    if (line.empty()) return fields;
    enum class S { StartField, InField, InQuoted, QuoteInQuoted } state = S::StartField;
    std::string field;
    for (char c : line) {
        switch (state) {
            case S::StartField:
                if (c == '"') state = S::InQuoted;
                else if (c == ',') fields.push_back(std::move(field)), field.clear();
                else field.push_back(c), state = S::InField;
                break;
            case S::InField:
                if (c == ',') fields.push_back(std::move(field)), field.clear(), state = S::StartField;
                else field.push_back(c);
                break;
            case S::InQuoted:
                if (c == '"') state = S::QuoteInQuoted;
                else field.push_back(c);
                break;
            case S::QuoteInQuoted:
                if (c == '"') field.push_back('"'), state = S::InQuoted;
                else if (c == ',') fields.push_back(std::move(field)), field.clear(), state = S::StartField;
                else field.push_back(c), state = S::InField;
                break;
        }
    }
    fields.push_back(std::move(field));
    return fields;
}

std::string format_line(const std::vector<std::string>& fields) {
    std::string out;
    for (std::size_t i = 0; i < fields.size(); ++i) {
        if (i) out.push_back(',');
        const auto& f = fields[i];
        bool quote = f.find_first_of(",\"\r\n") != std::string::npos;
        if (!quote) {
            out += f;
            continue;
        }
        out.push_back('"');
        for (char c : f) {
            if (c == '"') out.push_back('"');
            out.push_back(c);
        }
        out.push_back('"');
    }
    return out;
}

}  // namespace premarket::csv

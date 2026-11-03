#include "premarket/args.hpp"

#include "premarket/util.hpp"

namespace premarket::args {

bool Parsed::flag(const std::string& name) const {
    auto it = flags.find(name);
    return it != flags.end() && it->second;
}

std::optional<std::string> Parsed::value(const std::string& name) const {
    auto it = values.find(name);
    if (it == values.end() || it->second.empty()) return std::nullopt;
    return it->second.back();
}

std::vector<std::string> Parsed::all(const std::string& name) const {
    auto it = values.find(name);
    return it == values.end() ? std::vector<std::string>{} : it->second;
}

Parsed parse(const std::vector<std::string>& argv, const std::vector<Option>& options, std::size_t max_positionals) {
    Parsed out;
    for (std::size_t i = 0; i < argv.size(); ++i) {
        const std::string& a = argv[i];
        if (!util::starts_with(a, "--")) {
            if (out.positionals.size() >= max_positionals) throw UsageError{"unrecognized argument: " + a};
            out.positionals.push_back(a);
            continue;
        }
        std::string name = a.substr(2);
        std::optional<std::string> inline_value;
        if (auto eq = name.find('='); eq != std::string::npos) {
            inline_value = name.substr(eq + 1);
            name = name.substr(0, eq);
        }
        const Option* opt = nullptr;
        for (const auto& o : options)
            if (o.name == name) opt = &o;
        if (opt == nullptr) throw UsageError{"unrecognized option: --" + name};
        if (!opt->takes_value) {
            if (inline_value) throw UsageError{"--" + name + " takes no value"};
            out.flags[name] = true;
            continue;
        }
        std::string v;
        if (inline_value) {
            v = *inline_value;
        } else {
            if (i + 1 >= argv.size()) throw UsageError{"--" + name + " needs a value (" + opt->metavar + ")"};
            v = argv[++i];
        }
        auto& slot = out.values[name];
        if (!opt->repeatable && !slot.empty()) throw UsageError{"--" + name + " given twice"};
        slot.push_back(v);
    }
    return out;
}

std::string describe(const std::vector<Option>& options) {
    std::string out;
    for (const auto& o : options) {
        std::string left = "  --" + o.name + (o.takes_value ? "=" + o.metavar : "");
        if (o.repeatable) left += " (repeatable)";
        if (left.size() < 34) left.append(34 - left.size(), ' ');
        else left += "\n" + std::string(34, ' ');
        out += left + o.help + "\n";
    }
    return out;
}

}  // namespace premarket::args

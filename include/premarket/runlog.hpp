#pragma once

#include <filesystem>
#include <memory>
#include <string>

#include "premarket/log.hpp"

namespace premarket::runlog {

class Run {
public:
    Run(const std::string& binary, const std::string& date_dir);
    const std::filesystem::path& path() const { return path_; }

private:
    std::filesystem::path path_;
    std::unique_ptr<log::Tee> tee_;
};

}  // namespace premarket::runlog

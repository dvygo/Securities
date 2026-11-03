#include "premarket/runlog.hpp"

#include "premarket/paths.hpp"
#include "premarket/util.hpp"

namespace premarket::runlog {

Run::Run(const std::string& binary, const std::string& date_dir) {
    paths::ensure_log_dir();
    path_ = paths::logs_dir() / (binary + "_" + date_dir + "_" + util::local_now("%Y%m%d_%H%M%S") + ".log");
    tee_ = std::make_unique<log::Tee>(path_);
    log::err("Log: " + path_.string());
}

}  // namespace premarket::runlog

#pragma once

#include <string>
#include <vector>

#include "premarket/args.hpp"

namespace premarket::cli {

int main(int argc, char** argv);
int run(const std::vector<std::string>& argv);

std::vector<std::string> date_list(const std::string& raw);
std::string required_venue(const args::Parsed& p, const std::string& command);

}  // namespace premarket::cli

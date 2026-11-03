#pragma once

#include <string>
#include <vector>

namespace premarket::cli {

int main(int argc, char** argv);
int run(const std::vector<std::string>& argv);

std::vector<std::string> date_list(const std::string& raw);
std::vector<std::string> venue_selection(const std::vector<std::string>& raw);

}  // namespace premarket::cli

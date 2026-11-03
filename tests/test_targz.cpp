#include "test_targz.hpp"

#include <doctest/doctest.h>

#include <cstdlib>

#include "fixture.hpp"
#include "premarket/targz.hpp"

using namespace premarket;

TEST_CASE("an archive the system tar reads back byte for byte") {
    fixture::Tree tree;
    auto src = tree.root / "src";
    std::string deep = "20261103/TRANSFORM/manifests/" + std::string(90, 'x') + ".json";
    fs::create_directories((src / deep).parent_path());
    util::write_file(src / "a.txt", "alpha\n");
    util::write_file(src / deep, std::string(70000, 'z'));
    util::write_file(src / "20261103" / "empty.bin", "");
    auto archive = tree.root / "b.tar.gz";
    {
        targz::Writer w(archive);
        w.add(src / "a.txt", "a.txt");
        w.add(src / "20261103", "20261103");
        w.close();
    }
    auto out = tree.root / "out";
    fs::create_directories(out);
    std::string cmd = "tar -xzf '" + archive.string() + "' -C '" + out.string() + "'";
    REQUIRE(std::system(cmd.c_str()) == 0);
    CHECK(util::read_file(out / "a.txt") == "alpha\n");
    CHECK(util::read_file(out / deep) == std::string(70000, 'z'));
    CHECK(fs::exists(out / "20261103" / "empty.bin"));
    CHECK(fixture::files_under(out) == fixture::files_under(src));
}

TEST_CASE("a name that cannot fit ustar is refused") {
    fixture::Tree tree;
    auto file = tree.root / "f";
    util::write_file(file, "x");
    targz::Writer w(tree.root / "c.tar.gz");
    CHECK_THROWS_WITH(w.add(file, std::string(300, 'n')), doctest::Contains("name too long"));
}

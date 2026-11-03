#include "test_migrate.hpp"

#include <doctest/doctest.h>

#include "premarket/cli.hpp"
#include "premarket/normalize/migrate.hpp"
#include "premarket/qa.hpp"
#include "session_fixture.hpp"

using namespace premarket;

namespace {

const std::vector<std::string> kDays = {"20261102", "20261103"};
const std::vector<std::string> kVenues = {"XBOM", "XIMC", "XNSE"};

struct Upgrading : fixture::Tree {
    Upgrading() {
        fs::copy(fixture::test_data("state7x"), data(), fs::copy_options::recursive | fs::copy_options::overwrite_existing);
    }
    std::map<std::string, std::string> contents() const {
        std::map<std::string, std::string> out;
        for (const auto& f : fixture::files_under(data())) out[f] = util::read_file(data() / f);
        return out;
    }
};

using Entries = std::map<std::pair<std::string, std::string>, counter_token::VenueEntry>;

Entries entries(bool any_version) {
    Entries out;
    for (const auto& day : kDays)
        for (const auto& mic : kVenues) out[{day, mic}] = *counter_token::venue_entry(day, mic, any_version);
    return out;
}

void same_tokens(const Entries& a, const Entries& b) {
    REQUIRE(a.size() == b.size());
    for (const auto& [key, x] : a) {
        CAPTURE(key.first);
        CAPTURE(key.second);
        const auto& y = b.at(key);
        CHECK(x.assigned == y.assigned);
        CHECK(x.free == y.free);
        CHECK(x.retained == y.retained);
        CHECK(x.venue_id == y.venue_id);
    }
}

int normalize(const fixture::Tree& tree, const std::string& date) {
    return cli::run({"--config=" + tree.config_path.string(), "normalize", "--date-dir", date, "--only", "normalize-fyers"});
}

}  // namespace

TEST_CASE("a 7.x state is refused until it is migrated") {
    Upgrading tree;
    CHECK_THROWS_WITH_AS(state::read_head(), doctest::Contains("is a 7.x numbering state (head version 1)"),
                         state::StateCorrupt);
    CHECK_THROWS_WITH(counter_token::venue_entry("20261103", "XNSE"), doctest::Contains("migrate-state"));
    fixture::Stderr err;
    CHECK(normalize(tree, "20261103") == 1);
    CHECK(err.text().find("premarket migrate-state") != std::string::npos);
}

TEST_CASE("migrate-state: a dry run writes nothing") {
    Upgrading tree;
    auto before = tree.contents();
    CHECK(migrate::migrate_state("upgrade to 8.0.0", true) == 0);
    CHECK(tree.contents() == before);
}

TEST_CASE("migrate-state moves the 7.x state to v8 and changes no token") {
    Upgrading tree;
    auto before = entries(true);
    auto raw_head = jsonio::read(state::head_path());
    REQUIRE(raw_head["version"] == 1);
    std::map<std::string, std::string> snapshots;
    for (const auto& [mic, v] : raw_head["venues"].items())
        snapshots[mic] = util::read_file(state::snapshot_path(v["snapshot"].get<std::string>()));

    REQUIRE(migrate::migrate_state("upgrade to 8.0.0", false) == 0);

    auto head = state::read_head();
    REQUIRE(head);
    CHECK(head->version == 8);
    CHECK(head->counter == raw_head["counter"].get<std::int64_t>());
    for (const auto& [mic, v] : raw_head["venues"].items()) {
        CAPTURE(mic);
        const auto& vh = head->venues.at(mic);
        CHECK(vh.snapshot == v["snapshot"].get<std::string>());
        CHECK(vh.sha256 == v["sha256"].get<std::string>());
        CHECK(util::read_file(state::snapshot_path(vh.snapshot)) == snapshots.at(mic));
        CHECK(state::load_snapshot(mic, vh).assigned == before.at({"20261103", mic}).assigned);
    }
    same_tokens(entries(false), before);
    for (const auto& day : kDays) {
        CAPTURE(day);
        auto seq = jsonio::read(counter_token::sequence_path(day));
        CHECK(seq["version"] == 8);
        for (const auto& mic : kVenues) {
            CAPTURE(mic);
            auto header = jsonio::read(counter_token::venue_manifest_path(day, mic));
            CHECK(header["version"] == 8);
            CHECK(header["migrated"]["from_version"] == 5);
            CHECK(header["migrated"].contains("run_id"));
            CHECK(header["migrated"].contains("at"));
            CHECK(header["migrated"].contains("by"));
        }
    }
    CHECK(counter_token::load_sequence("20261103") == head->counter);
    CHECK(session_fixture::event_types("migrate-state") == std::vector<std::string>{"START", "COMPLETE"});
    CHECK(session_fixture::backups() == 1);
    std::vector<std::string> failures;
    for (const auto& c : qa::state_checks())
        if (!c.ok && c.hard) failures.push_back(c.name + ": " + c.detail);
    CHECK(failures.empty());

    auto migrated = tree.contents();
    CHECK(migrate::migrate_state("again", false) == 0);
    CHECK(tree.contents() == migrated);
}

TEST_CASE("8.0.0 carries the 7.x numbering on") {
    Upgrading tree;
    auto seven = entries(true);
    REQUIRE(migrate::migrate_state("upgrade to 8.0.0", false) == 0);
    auto counter = state::read_head()->counter;

    REQUIRE(normalize(tree, "20261103") == 0);
    for (const auto& mic : kVenues) {
        CAPTURE(mic);
        CHECK(counter_token::venue_entry("20261103", mic)->assigned == seven.at({"20261103", mic}).assigned);
        CHECK_FALSE(jsonio::read(counter_token::venue_manifest_path("20261103", mic)).contains("migrated"));
    }

    fs::create_directories(tree.data() / "20261104");
    for (const auto& mic : kVenues)
        fs::copy(tree.data() / "20261103" / mic, tree.data() / "20261104" / mic, fs::copy_options::recursive);
    REQUIRE(normalize(tree, "20261104") == 0);
    for (const auto& mic : kVenues) {
        CAPTURE(mic);
        auto next = counter_token::venue_entry("20261104", mic)->assigned;
        CHECK_FALSE(next.empty());
        for (const auto& [script, token] : next) {
            CAPTURE(script);
            CHECK(seven.at({"20261103", mic}).assigned.at(script) == token);
        }
    }
    CHECK(state::read_head()->counter == counter);
}

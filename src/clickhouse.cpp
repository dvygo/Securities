#include "premarket/clickhouse.hpp"

#include <httplib.h>

#include <set>

#include "premarket/export.hpp"
#include "premarket/log.hpp"
#include "premarket/paths.hpp"
#include "premarket/util.hpp"

namespace premarket::clickhouse {

namespace {

const std::set<std::string> kIntColumns = {"multiplier", "lotSize",      "tickSize",       "expiration",
                                           "scriptToken", "strike",      "counterToken",   "counterTokenV2",
                                           "enable1",    "enable2"};

std::string q(const std::string& ident) { return "\"" + util::replace_all(ident, "\"", "\\\"") + "\""; }

std::string column_ddl(const std::vector<std::pair<std::string, std::string>>& cols) {
    std::vector<std::string> parts;
    for (const auto& [name, type] : cols) parts.push_back(q(name) + " " + type);
    return util::join(parts, ",\n    ");
}

std::vector<std::pair<std::string, std::string>> contract_columns() {
    std::vector<std::pair<std::string, std::string>> out;
    for (const auto& c : paths::CONTRACT_COLUMNS) out.push_back({c, is_int_column(c) ? "Nullable(Int64)" : "String"});
    return out;
}

const std::vector<std::pair<std::string, std::string>> kBasketColumns = {
    {"date", "String"}, {"basket", "String"}, {"scripts", "Array(String)"}};

std::string staging(const std::string& table) { return table + "_staging"; }

void drop_create(Client& c, const std::string& db, const std::string& table,
                 const std::vector<std::pair<std::string, std::string>>& cols, const std::string& order_by) {
    c.command("DROP TABLE IF EXISTS " + q(db) + "." + q(table));
    c.command("CREATE TABLE " + q(db) + "." + q(table) + " (\n    " + column_ddl(cols) + "\n) ENGINE = MergeTree ORDER BY (" +
              order_by + ") SETTINGS " + COMPACT_PARTS_SETTINGS);
}

void ensure_target(Client& c, const std::string& db, const std::string& table,
                   const std::vector<std::pair<std::string, std::string>>& cols, const std::string& order_by) {
    c.command("CREATE TABLE IF NOT EXISTS " + q(db) + "." + q(table) + " (\n    " + column_ddl(cols) +
              "\n) ENGINE = MergeTree ORDER BY (" + order_by + ") SETTINGS " + COMPACT_PARTS_SETTINGS);
}

void swap_into_place(Client& c, const std::string& db, const std::string& stage, const std::string& target) {
    try {
        c.command("EXCHANGE TABLES " + q(db) + "." + q(stage) + " AND " + q(db) + "." + q(target));
    } catch (const std::exception& e) {
        throw RuntimeError("EXCHANGE TABLES failed for " + db + "." + target + ": " + e.what() + ". The new rows are in " +
                           db + "." + stage + " and " + db + "." + target +
                           " still holds the previous load. EXCHANGE requires an Atomic database engine (the default "
                           "since ClickHouse 20.10); check with SELECT engine FROM system.databases WHERE name = '" +
                           db + "'.");
    }
    c.command("DROP TABLE IF EXISTS " + q(db) + "." + q(stage));
}

std::int64_t push_contracts(Client& c, const std::string& db, const std::string& date_dir, bool update_current) {
    std::string dated = contracts_table(date_dir);
    auto cols = contract_columns();
    std::string order_by = "\"exchange\", \"script\"";
    ensure_target(c, db, dated, cols, order_by);
    drop_create(c, db, staging(dated), cols, order_by);
    std::int64_t total = 0;
    exporter::iter_contract_rows(date_dir, EXPORT_BATCH_ROWS, [&](const std::vector<std::vector<std::string>>& batch) {
        if (batch.empty()) return;
        std::string body;
        for (const auto& row : batch) {
            jsonio::Json obj = jsonio::Json::object();
            for (std::size_t i = 0; i < paths::CONTRACT_COLUMNS.size(); ++i)
                obj[paths::CONTRACT_COLUMNS[i]] = cell(paths::CONTRACT_COLUMNS[i], row[i]);
            body += jsonio::line(obj) + "\n";
        }
        c.insert_json(staging(dated), paths::CONTRACT_COLUMNS, body);
        total += static_cast<std::int64_t>(batch.size());
        log::out("      " + std::to_string(total) + " row(s) -> " + db + "." + dated + "...");
    });
    if (!total) {
        log::out("    No contracts to push for " + date_dir);
        c.command("DROP TABLE IF EXISTS " + q(db) + "." + q(staging(dated)));
        return 0;
    }
    swap_into_place(c, db, staging(dated), dated);
    log::out("    Pushed " + std::to_string(total) + " rows -> " + db + "." + dated);
    if (!update_current) {
        log::out("    Not touching " + db + ".contracts: " + date_dir + " is older than the newest live day");
        return total;
    }
    ensure_target(c, db, "contracts", cols, order_by);
    drop_create(c, db, staging("contracts"), cols, order_by);
    std::string n = std::to_string(EXPORT_BATCH_ROWS);
    c.command("INSERT INTO " + q(db) + "." + q(staging("contracts")) + " SELECT * FROM " + q(db) + "." + q(dated),
              {{"max_block_size", n}, {"max_insert_block_size", n}, {"min_insert_block_size_rows", n},
               {"min_insert_block_size_bytes", "0"}});
    swap_into_place(c, db, staging("contracts"), "contracts");
    log::out("    Pushed " + std::to_string(total) + " rows -> " + db + ".contracts (current mirror)");
    return total;
}

std::int64_t push_baskets(Client& c, const std::string& db, const std::string& date_dir, bool update_current) {
    std::vector<std::string> order;
    std::map<std::string, std::vector<std::string>> grouped;
    for (const auto& row : exporter::aggregate_basket_rows(date_dir)) {
        if (row.basket.empty() || row.script.empty()) continue;
        if (!grouped.count(row.basket)) order.push_back(row.basket);
        grouped[row.basket].push_back(row.script);
    }
    if (grouped.empty()) {
        log::out("    No baskets to push for " + date_dir);
        return 0;
    }
    std::vector<std::string> targets = {baskets_table(date_dir)};
    if (update_current) targets.push_back("baskets");
    for (const auto& target : targets) {
        ensure_target(c, db, target, kBasketColumns, "\"basket\"");
        drop_create(c, db, staging(target), kBasketColumns, "\"basket\"");
        for (const auto& name : order) {
            jsonio::Json obj = {{"date", date_dir}, {"basket", name}, {"scripts", grouped[name]}};
            c.insert_json(staging(target), {"date", "basket", "scripts"}, jsonio::line(obj) + "\n");
        }
        swap_into_place(c, db, staging(target), target);
        log::out("    Pushed " + std::to_string(order.size()) + " baskets -> " + db + "." + target);
    }
    return static_cast<std::int64_t>(order.size());
}

}  // namespace

bool is_int_column(const std::string& column) { return kIntColumns.count(column) > 0; }

jsonio::Json cell(const std::string& column, const std::string& value) {
    if (!is_int_column(column)) return value;
    if (value.empty()) return nullptr;
    auto n = util::parse_int(value);
    if (!n) return nullptr;
    return *n;
}

std::string contracts_table(const std::string& date_dir) { return "contracts_" + paths::validate_date_dir(date_dir); }
std::string baskets_table(const std::string& date_dir) { return "baskets_" + paths::validate_date_dir(date_dir); }

Client::Client(Config cfg) : cfg_(std::move(cfg)) {}

namespace {

std::string url_encode(const std::string& s) {
    static const char* hex = "0123456789ABCDEF";
    std::string out;
    for (unsigned char c : s) {
        if (std::isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') out.push_back(static_cast<char>(c));
        else out += std::string("%") + hex[c >> 4] + hex[c & 0xF];
    }
    return out;
}

std::string post(const Config& cfg, const std::string& query_params, const std::string& body) {
    std::string origin = (cfg.secure ? "https://" : "http://") + cfg.host + ":" + std::to_string(cfg.port);
    httplib::Client client(origin);
    client.set_connection_timeout(30, 0);
    client.set_read_timeout(600, 0);
    client.set_write_timeout(600, 0);
    client.set_basic_auth(cfg.username, cfg.password);
    auto res = client.Post("/?" + query_params, body, "text/plain; charset=utf-8");
    if (!res) throw RuntimeError("clickhouse " + origin + ": " + httplib::to_string(res.error()));
    if (res->status != 200) throw RuntimeError("clickhouse HTTP " + std::to_string(res->status) + ": " + util::strip(res->body));
    return res->body;
}

}  // namespace

std::string Client::command(const std::string& sql, const std::map<std::string, std::string>& settings, bool use_database) {
    std::string params = "async_insert=0";
    if (use_database) params += "&database=" + url_encode(cfg_.database);
    for (const auto& [k, v] : settings) params += "&" + url_encode(k) + "=" + url_encode(v);
    return post(cfg_, params, sql);
}

void Client::insert_json(const std::string& table, const std::vector<std::string>& columns, const std::string& body) {
    std::vector<std::string> cols;
    for (const auto& c : columns) cols.push_back(q(c));
    std::string sql = "INSERT INTO " + q(cfg_.database) + "." + q(table) + " (" + util::join(cols, ", ") + ") FORMAT JSONEachRow";
    post(cfg_, "async_insert=0&database=" + url_encode(cfg_.database) + "&query=" + url_encode(sql), body);
}

Result push(const Config& cfg, const std::string& date_dir, bool update_current) {
    log::out("  Pushing to ClickHouse " + cfg.host + ":" + std::to_string(cfg.port) + "/" + cfg.database + "...");
    Client c(cfg);
    c.command("CREATE DATABASE IF NOT EXISTS " + q(cfg.database), {}, false);
    Result r;
    r.contracts = push_contracts(c, cfg.database, date_dir, update_current);
    r.baskets = push_baskets(c, cfg.database, date_dir, update_current);
    std::vector<std::string> tables = {contracts_table(date_dir), baskets_table(date_dir)};
    if (update_current) {
        tables.push_back("contracts");
        tables.push_back("baskets");
    }
    for (const auto& t : tables) r.tables.push_back(cfg.database + "." + t);
    log::out("  Successfully pushed to " + cfg.database);
    return r;
}

}  // namespace premarket::clickhouse

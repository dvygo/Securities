#pragma once
// premarket/config.hpp -- the v8 config.ini and keys.ini, loaded once per run.
//
// The CLI loads both from --config and --keys (absolute paths, always) before any command
// runs; everything else reads config::get(). There are no environment overrides and no
// search for a repo root: the files named on the command line are the whole configuration,
// which is what makes a run reproducible from its command line alone.
//
// conf/config.ini.example writes every path as @DIR@/...: install.sh seeds DIR/conf/config.ini
// from it with the directory it installs into in place of @DIR@, and the test does the same.
//
// config.ini sections (anything else is an error):
//   [premarket] config_version = 8   a 7.x file has no such section and is refused with a
//                                    pointer to the example, never half-read
//   [paths]    data_dir contracts_dir logs_dir reports_dir baskets_dir sinks_dir, all absolute
//   [run]      timezone              the IANA zone "today" is taken in (validated at load)
//   [baskets]  enabled = 0|1         a deployment property, deliberately not a CLI flag
//   [fyers]    base_url user_agent timeout_sec retries retry_delay_sec
//   [venue.<MIC>] feed enabled venue_id + for databento: dataset stype_in symbols
//
// keys.ini: one [databento] section, MIC = key. databento_key() names the file and the
// missing MIC when a download needs a key it does not have; a run that downloads nothing
// from Databento never needs --keys at all.
//
// feed_for / owns answer "which feed serves this MIC today": a normalize step runs a venue
// only when its feed owns it, so switching XNSE from fyers to nse moves it between steps
// without anything being written twice. today() is the date in [run] timezone.

//! A terminal viewer for the formats this pipeline writes: Parquet, DBN(.zst)
//! and raw vendor CSV.
//!
//! Exists because the day's artefacts are large (OPRA normalizes to ~2M rows,
//! 120MB of zstd Parquet) and opening one just to check a few columns meant
//! spinning up Python and a DataFrame. Rows are read lazily up to --limit and
//! only the visible window is ever formatted, so startup does not depend on
//! file size.
//!
//! Keys: arrows / hjkl move, PgUp/PgDn page, g/G jump to top/bottom,
//!       0/$ jump to first/last column, q or Esc quits.

mod source;

use anyhow::{Context, Result};
use crossterm::{
    event::{self, Event, KeyCode, KeyEventKind},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    prelude::*,
    widgets::{Block, Borders, Cell, Paragraph, Row, Table as TableWidget},
};
use source::Table;
use std::{path::PathBuf, time::Duration};

const DEFAULT_LIMIT: usize = 100_000;
const COL_WIDTH: u16 = 22;

struct App {
    table: Table,
    row: usize,
    col: usize,
}

impl App {
    fn visible_rows(&self, height: usize) -> usize { height.saturating_sub(4).max(1) }

    fn move_row(&mut self, delta: isize) {
        let max = self.table.rows.len().saturating_sub(1);
        self.row = (self.row as isize + delta).clamp(0, max as isize) as usize;
    }

    fn move_col(&mut self, delta: isize) {
        let max = self.table.header.len().saturating_sub(1);
        self.col = (self.col as isize + delta).clamp(0, max as isize) as usize;
    }
}

fn main() -> Result<()> {
    let mut args = std::env::args().skip(1);
    let mut path: Option<PathBuf> = None;
    let mut limit = DEFAULT_LIMIT;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--limit" | "-n" => {
                limit = args.next().context("--limit needs a value")?.parse()?;
            }
            "--help" | "-h" => {
                println!("rust-viewer <file.parquet|file.dbn.zst|file.csv> [--limit N]");
                return Ok(());
            }
            other => path = Some(PathBuf::from(other)),
        }
    }
    let path = path.context("usage: rust-viewer <file> [--limit N]")?;
    let table = Table::load(&path, limit)?;

    enable_raw_mode()?;
    let mut out = std::io::stdout();
    execute!(out, EnterAlternateScreen)?;
    let mut terminal = Terminal::new(CrosstermBackend::new(out))?;

    let result = run(&mut terminal, App { table, row: 0, col: 0 });

    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()?;
    result
}

fn run<B: Backend>(terminal: &mut Terminal<B>, mut app: App) -> Result<()> {
    loop {
        terminal.draw(|frame| draw(frame, &app))?;
        if !event::poll(Duration::from_millis(200))? { continue; }
        if let Event::Key(key) = event::read()? {
            if key.kind != KeyEventKind::Press { continue; }
            let page = app.visible_rows(terminal.size()?.height as usize) as isize;
            match key.code {
                KeyCode::Char('q') | KeyCode::Esc => return Ok(()),
                KeyCode::Down | KeyCode::Char('j') => app.move_row(1),
                KeyCode::Up | KeyCode::Char('k') => app.move_row(-1),
                KeyCode::Right | KeyCode::Char('l') => app.move_col(1),
                KeyCode::Left | KeyCode::Char('h') => app.move_col(-1),
                KeyCode::PageDown => app.move_row(page),
                KeyCode::PageUp => app.move_row(-page),
                KeyCode::Char('g') | KeyCode::Home => app.row = 0,
                KeyCode::Char('G') | KeyCode::End => app.move_row(isize::MAX / 2),
                KeyCode::Char('0') => app.col = 0,
                KeyCode::Char('$') => app.move_col(isize::MAX / 2),
                _ => {}
            }
        }
    }
}

fn draw(frame: &mut Frame, app: &App) {
    let area = frame.area();
    let chunks = Layout::vertical([Constraint::Min(3), Constraint::Length(1)]).split(area);

    // Only the columns that fit are formatted -- a 40-column, 2M-row file must
    // cost the same to render as a small one.
    let per_screen = ((chunks[0].width / COL_WIDTH).max(1)) as usize;
    let first_col = app.col.saturating_sub(per_screen.saturating_sub(1));
    let cols: Vec<usize> = (first_col..app.table.header.len()).take(per_screen).collect();

    let visible = app.visible_rows(chunks[0].height as usize + 4);
    let first_row = app.row.saturating_sub(visible.saturating_sub(1)).min(
        app.table.rows.len().saturating_sub(visible.min(app.table.rows.len())),
    );

    let header = Row::new(cols.iter().map(|&c| {
        let style = if c == app.col { Style::new().bold().reversed() } else { Style::new().bold() };
        Cell::from(clip(&app.table.header[c])).style(style)
    }));

    let rows: Vec<Row> = app.table.rows[first_row..]
        .iter()
        .take(visible)
        .enumerate()
        .map(|(i, r)| {
            let selected = first_row + i == app.row;
            let cells = cols.iter().map(|&c| Cell::from(clip(r.get(c).map(|s| s.as_str()).unwrap_or(""))));
            let row = Row::new(cells);
            if selected { row.style(Style::new().reversed()) } else { row }
        })
        .collect();

    let widths = vec![Constraint::Length(COL_WIDTH - 1); cols.len()];
    frame.render_widget(
        TableWidget::new(rows, widths).header(header).block(
            Block::default().borders(Borders::ALL).title(app.table.origin.clone()),
        ),
        chunks[0],
    );

    let status = format!(
        " row {}/{}   col {}/{} {}   q quit · hjkl/arrows move · PgUp/PgDn page · g/G top/bottom ",
        if app.table.rows.is_empty() { 0 } else { app.row + 1 },
        app.table.rows.len(),
        app.col + 1,
        app.table.header.len(),
        app.table.header.get(app.col).map(|s| format!("({s})")).unwrap_or_default(),
    );
    frame.render_widget(Paragraph::new(status).style(Style::new().reversed()), chunks[1]);
}

/// Cells are clipped to the column width at render time so a long OCC symbol or
/// a definition blob cannot break the layout.
fn clip(value: &str) -> String {
    let width = (COL_WIDTH - 2) as usize;
    if value.chars().count() <= width {
        value.to_string()
    } else {
        let mut s: String = value.chars().take(width - 1).collect();
        s.push('…');
        s
    }
}

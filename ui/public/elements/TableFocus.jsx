// Persistent left panel (Chainlit ElementSidebar), replacing the settings-drawer MultiSelect.
// Props come from ui/app.py:
//   { schema: str, groups: [{label, tables: [{name, columns: [str]}]}], selected: [str],
//     columnInfo: {"table.column": {fill_pct, sample: [...]} | {fill_pct, masked: true} | {error}} }
// Checking a table both re-renders locally (updateElement) and tells the Python session which
// tables are focused (callAction -> @cl.action_callback("set_focus_tables") in app.py). Clicking
// "(i)" on a column calls column_info in app.py, which fetches fill rate and a sample and pushes
// the result back with element.update() (a backend-initiated update, not this component's own
// updateElement) — so a column's info can arrive well after this component last rendered.
// collapsedGroups/expandedTables are UI-only state, round-tripped the same way as everything
// else here, but never sent to Python: purely cosmetic, not part of what the model is told.
// Text only, no icons/emoji: [-]/[+] are literal characters, not glyphs, and every group kind is
// spelled out rather than pictured.
export default function TableFocus() {
  const groups = props.groups || [];
  const selected = new Set(props.selected || []);
  const collapsed = new Set(props.collapsedGroups || []);
  const expandedTables = new Set(props.expandedTables || []);
  const columnInfo = props.columnInfo || {};

  function publish(next) {
    const list = Array.from(next);
    updateElement({ ...props, selected: list });
    callAction({ name: "set_focus_tables", payload: { selected: list } });
  }

  function toggle(table) {
    const next = new Set(selected);
    next.has(table) ? next.delete(table) : next.add(table);
    publish(next);
  }

  function toggleGroup(label) {
    const next = new Set(collapsed);
    next.has(label) ? next.delete(label) : next.add(label);
    updateElement({ ...props, collapsedGroups: Array.from(next) });
  }

  function toggleTable(name) {
    const next = new Set(expandedTables);
    next.has(name) ? next.delete(name) : next.add(name);
    updateElement({ ...props, expandedTables: Array.from(next) });
  }

  function describe(info) {
    if (!info) return null;
    if (info.error) return info.error;
    if (info.masked) return `${info.fill_pct}% filled (values hidden: personal data)`;
    const sample = (info.sample || []).length > 0 ? info.sample.join(", ") : "(no values)";
    return `${info.fill_pct}% filled. Sample: ${sample}`;
  }

  return (
    <div className="text-sm space-y-3 p-1">
      <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Schema: {props.schema || "tables"}
      </div>

      <div className="text-xs border rounded p-2">
        <span className="font-medium">Selected: </span>
        {selected.size > 0 ? Array.from(selected).sort().join(", ") : "none (no preference set)"}
        {selected.size > 0 && (
          <button
            type="button"
            className="ml-2 underline text-muted-foreground hover:text-foreground"
            onClick={() => publish(new Set())}
          >
            clear
          </button>
        )}
      </div>

      {groups.map((g) => {
        const isOpen = !collapsed.has(g.label);
        return (
          <div key={g.label}>
            <button
              type="button"
              onClick={() => toggleGroup(g.label)}
              className="flex items-center gap-1 text-xs font-medium text-muted-foreground mb-1 w-full hover:text-foreground"
            >
              <span>{isOpen ? "[-]" : "[+]"}</span>
              <span>{g.label} ({g.tables.length})</span>
            </button>
            {isOpen && (
              <div className="space-y-1 pl-4">
                {g.tables.map((t) => {
                  const tableOpen = expandedTables.has(t.name);
                  return (
                    <div key={t.name}>
                      <div className="flex items-center gap-2">
                        <input type="checkbox" checked={selected.has(t.name)} onChange={() => toggle(t.name)} />
                        <span className="font-mono text-xs">{t.name}</span>
                        {t.columns.length > 0 && (
                          <button
                            type="button"
                            className="text-xs underline text-muted-foreground hover:text-foreground"
                            onClick={() => toggleTable(t.name)}
                          >
                            {tableOpen ? "hide columns" : `show columns (${t.columns.length})`}
                          </button>
                        )}
                      </div>
                      {tableOpen && (
                        <div className="pl-6 space-y-1 mt-1">
                          {t.columns.map((c) => {
                            const info = columnInfo[`${t.name}.${c}`];
                            return (
                              <div key={c} className="text-xs">
                                <span className="font-mono">{c}</span>
                                <button
                                  type="button"
                                  className="ml-1 underline text-muted-foreground hover:text-foreground"
                                  onClick={() => callAction({ name: "column_info", payload: { table: t.name, column: c } })}
                                >
                                  (i)
                                </button>
                                {info && <div className="pl-2 text-muted-foreground">{describe(info)}</div>}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}

      <div className="text-xs text-muted-foreground pt-2 border-t">
        A preference, not a restriction — checked tables are ranked first, but the assistant
        may still look elsewhere if none of them can answer the question.
      </div>
    </div>
  );
}

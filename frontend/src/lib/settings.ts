// Types for `GET /api/settings` and `PUT /api/settings/{key}`.
//
// The backend registry (app/services/settings_store.py) is the single authority for bounds, units
// and defaults — they are SERVED, not duplicated here. A second copy in TypeScript would drift, and
// the copy that drifts is always the one the operator is looking at when they set a value the API
// then rejects.

export interface Parameter {
  key: string;
  label: string;
  group: string;
  /** "pp" | "%" | "x" | "$B" | "count" | "s" — rendered beside the input. */
  unit: string;
  value: number;
  default: number;
  min: number;
  max: number;
  help: string;
  /** Where a change shows up, so the effect of editing is not a guess. */
  used_by: string;
  is_default: boolean;
}

export interface SettingChange {
  key: string;
  label: string;
  old_value: number | null;
  new_value: number;
  changed_at: string;
  changed_by: string | null;
}

export interface SettingsResponse {
  meta: {
    /** "database" | "defaults". "defaults" means the DB could not be read and every value shown is
     *  the compiled default — NOT that anyone chose them. The page must say so. */
    source: string;
    count: number;
    /** Values the dashboard deliberately does not own: docs/SLATE.md is their authority. */
    document_sourced: { label: string; value_note: string }[];
  };
  parameters: Parameter[];
  history: SettingChange[];
}

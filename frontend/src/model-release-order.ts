import releaseDates from './model-release-dates.json' with { type: 'json' };

const dates: Readonly<Record<string, string>> = releaseDates;
const knownDate = (name: string): string | null => (Object.hasOwn(dates, name) ? dates[name] : null);

/** Resolve known provider aliases without changing the actual model ID. */
export function modelReleaseDate(model: string): string | null {
  let name = model.trim();
  if (knownDate(name)) return knownDate(name);
  name = name
    .replace(/^(?:publishers\/)?(?:openai|anthropic|google)\//, '')
    .replace(/^(?:(?:us|eu|apac|global)\.)?anthropic\./, '')
    .replace(/-v\d+:\d+$/, '')
    .replace(/^(claude-(?:haiku|sonnet|opus|fable)-\d+)\.(\d+)/, '$1-$2')
    .replace(/^(claude-\d+)\.(\d+)-/, '$1-$2-')
    .replace(/@(?=\d{8}$)/, '-');
  if (knownDate(name)) return knownDate(name);
  // These local reasoning variants share the underlying model's release.
  const base = name.replace(/(?:-(?:thinking|max|xhigh|high|medium|low|none|minimal))+$/, '');
  return knownDate(base);
}

/** Newest verified releases first; unknown dates and equal dates keep their input order. */
export function sortModelsByReleaseDate(models: readonly string[]): string[] {
  return [...new Set(models.map((model) => model.trim()))]
    .map((model) => ({ model, date: modelReleaseDate(model) || '' }))
    .sort((left, right) => right.date.localeCompare(left.date))
    .map(({ model }) => model);
}

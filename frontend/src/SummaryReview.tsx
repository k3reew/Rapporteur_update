import { useEffect, useState, useRef, type ReactNode } from 'react';

import { Recommendations } from './Departments';
import type { TalkProgress } from './talkTypes';
import { request } from './api';
import { statusTone } from './status';
import { CardEditor, CardView, cleanCard, type Card } from './TalkCard';
import { talkStatuses } from './status';

type Line = { text: string; claim_ids: string[] };
type Evidence = { label: string; quote: string; fragment_revision_id: string; span: [number, number] };
type Rejected = { label: string; quote: string; reason: string };
type ClaimStatus = 'rejected' | 'verified' | 'needs_human' | 'confirmed';
type Claim = {
  key: string;
  type: string;
  text: string;
  status: ClaimStatus;
  evidence: Evidence[];
  rejected_evidence: Rejected[];
};
type Locator =
  | { kind: 'page'; page_no: number }
  | { kind: 'time'; start_ms: number; end_ms: number }
  | { kind: 'lines'; line_start: number; line_end: number };
type Fragment = {
  label: string;
  kind: string;
  revision_id: string;
  source_file_id: string;
  locator: Locator;
  text: string;
};
type Summary = {
  version: number;
  status: 'ready' | 'needs_attention' | 'stale';
  profile: string;
  claims: Claim[];
  sentences: Line[];
  conclusions: Line[];
  human_edit: boolean;
  created_at: string;
};
export type SummaryView = {
  summary: Summary;
  pack: { fragments: Fragment[] };
  talk_title: string | null;
  talk_status: string;
};
type Draft = { sentences: Line[]; conclusions: Line[]; claims: Record<string, string> };

const claimTypes: Record<string, string> = {
  problem: 'задача',
  method: 'метод',
  result: 'результат',
  conclusion: 'вывод',
  listener_remark: 'реплика слушателя',
};
const claimStatuses: Record<ClaimStatus, string> = {
  rejected: 'цитата не найдена',
  verified: 'цитата найдена, не проверено человеком',
  needs_human: 'нужна проверка человеком',
  confirmed: 'подтверждено',
};
const claimTone: Record<ClaimStatus, string> = {
  rejected: 'bad',
  verified: 'info',
  needs_human: 'warn',
  confirmed: 'ok',
};
const rejections: Record<string, string> = {
  label_not_found: 'метки нет в материалах',
  quote_not_found: 'цитата не найдена',
  below_threshold: 'совпадение слишком слабое',
  number_mismatch: 'не совпали числа',
  negation_mismatch: 'не совпало отрицание',
  too_short: 'цитата слишком короткая',
};
const json = { 'Content-Type': 'application/json' };

/** Диапазон цитаты считается в кодовых точках (ADR-003), а строки JS — в UTF-16:
 * Array.from режет по кодовым точкам, поэтому символы вне базовой плоскости не ломают подсветку. */
export function highlight(text: string, [start, end]: [number, number]): ReactNode {
  const points = Array.from(text);
  return (
    <>
      {points.slice(0, start).join('')}
      <mark>{points.slice(start, end).join('')}</mark>
      {points.slice(end).join('')}
    </>
  );
}

function place(fragment: Fragment) {
  const { locator } = fragment;
  if (locator.kind === 'page') return `слайд ${locator.page_no}`;
  if (locator.kind === 'lines') return `строки ${locator.line_start}–${locator.line_end}`;
  return `с ${Math.floor(locator.start_ms / 1000)} с`;
}

export function SummaryReview(
  { talkId, title, onClose, conferenceId, onNavigate }: {
    talkId: string; title?: string; onClose: () => void; conferenceId?: string;
    onNavigate?: (id: string) => void;
  },
) {
  const url = `/api/v1/talks/${talkId}/summary`;
  const [view, setView] = useState<SummaryView>();
  const [selected, setSelected] = useState('');
  const [draft, setDraft] = useState<Draft>();
  const [busy, setBusy] = useState('');
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  const [talks, setTalks] = useState<TalkProgress[]>([]);
  const [metadata, setMetadata] = useState<Omit<Card, 'title'> & { title: string | null; revision: number }>();
  const [metaDraft, setMetaDraft] = useState<Card>();
  const [pending, setPending] = useState<string>();
  const allowNavigation = useRef(false);
  useEffect(() => {
    if (!conferenceId) return;
    const controller = new AbortController();
    void request(`/api/v1/conferences/${conferenceId}/progress`, { signal: controller.signal })
      .then(async r => setTalks((await r.json()).talks))
      .catch((e: Error) => { if (!controller.signal.aborted) setError(e.message); });
    void request(`/api/v1/talks/${talkId}`, { signal: controller.signal })
      .then(async r => setMetadata(await r.json()))
      .catch((e: Error) => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [conferenceId, talkId]);
  useEffect(() => {
    function guard(e: BeforeUnloadEvent) { if (draft || metaDraft) e.preventDefault(); }
    function routeGuard(e: Event) {
      if (allowNavigation.current) { allowNavigation.current = false; return; }
      if (draft || metaDraft) { e.preventDefault(); setPending('hash:' + (e as CustomEvent<string>).detail); }
    }
    window.addEventListener('beforeunload', guard);
    window.addEventListener('rapporteur-navigate', routeGuard);
    return () => { window.removeEventListener('beforeunload', guard); window.removeEventListener('rapporteur-navigate', routeGuard); };
  }, [draft, metaDraft]);
  function navigate(id: string) {
    if (draft || metaDraft) { setPending(id); return; }
    if (id === 'close') onClose(); else onNavigate?.(id);
  }
  function leave(id: string) {
    allowNavigation.current = true;
    if (id.startsWith('hash:')) window.location.hash = id.slice(5);
    else if (id === 'close') onClose(); else onNavigate?.(id);
  }
  async function saveMetadata() {
    if (!metaDraft || !metadata) return;
    const card = cleanCard(metaDraft);
    await request(`/api/v1/talks/${talkId}`, { method: 'PUT', headers: json,
      body: JSON.stringify({ title: card.title || null, speakers: card.speakers,
        organizations: card.organizations, contacts: card.contacts, revision: metadata.revision }),
    });
    setMetadata(await (await request(`/api/v1/talks/${talkId}`)).json()); setMetaDraft(undefined);
  }

  async function read(response: Response) {
    setView(await response.json());
  }

  useEffect(() => {
    const controller = new AbortController();
    void request(url, { signal: controller.signal })
      .then(read)
      .catch((reason: Error) => {
        if (!controller.signal.aborted) setError(reason.message);
      });
    return () => controller.abort();
  }, [url]);

  async function act(label: string, done: string, call: () => Promise<Response>) {
    setBusy(label);
    setError('');
    setStatus('');
    try {
      await read(await call());
      setDraft(undefined);
      setStatus(done);
      return true;
    } catch (reason) {
      setError((reason as Error).message);
      return false;
    } finally {
      setBusy('');
    }
  }

  if (!view) {
    return (
      <section aria-label="Проверка сводки">
        <p><button className="link" onClick={() => navigate('close')}><span aria-hidden="true">← </span>К списку докладов</button></p>
        {error ? <p role="alert">{error}</p> : <p className="muted"><span className="spinner" aria-hidden="true" />Загрузка сводки…</p>}
      </section>
    );
  }

  const { summary, pack, talk_status: talkStatus } = view;
  const claims = new Map(summary.claims.map((claim) => [claim.key, claim]));
  const fragments = new Map(pack.fragments.map((fragment) => [fragment.revision_id, fragment]));
  const byLabel = new Map(pack.fragments.map((fragment) => [fragment.label, fragment]));
  const claim = claims.get(selected);
  const used = new Set([...summary.sentences, ...summary.conclusions].flatMap((line) => line.claim_ids));

  function startEdit() {
    setDraft({
      sentences: summary.sentences.map((line) => ({ ...line })),
      conclusions: summary.conclusions.map((line) => ({ ...line })),
      claims: Object.fromEntries(summary.claims.map((item) => [item.key, item.text])),
    });
  }

  async function save() {
    if (!draft) return;
    const edited = summary.claims
      .filter((item) => draft.claims[item.key].trim() && draft.claims[item.key] !== item.text)
      .map((item) => ({ key: item.key, text: draft.claims[item.key].trim() }));
    const lines = (items: Line[]) => items
      .map((line) => ({ ...line, text: line.text.trim() }))
      .filter((line) => line.text);
    return await act('Сохранение…', 'Правка сохранена, создана новая версия.', () => request(url, {
      method: 'PUT',
      headers: json,
      body: JSON.stringify({
        version: summary.version,
        sentences: lines(draft.sentences),
        conclusions: lines(draft.conclusions),
        claims: edited,
      }),
    }));
  }

  function lineEditor(kind: 'sentences' | 'conclusions', label: string) {
    if (!draft) return null;
    return draft[kind].map((line, index) => (
      <li key={index}>
        <textarea
          aria-label={`${label} ${index + 1}`}
          value={line.text}
          rows={3}
          cols={80}
          disabled={!!busy}
          onChange={(event) => {
            const text = event.target.value;
            setDraft((previous) => previous && ({
              ...previous,
              [kind]: previous[kind].map((item, position) => (position === index ? { ...item, text } : item)),
            }));
          }}
        />
        <br /><small>Опирается на {line.claim_ids.join(', ')}</small>
      </li>
    ));
  }

  function lineView(line: Line, index: number) {
    return (
      <li key={index}>
        {line.text}{' '}
        {line.claim_ids.map((key) => (
          <button
            key={key}
            className="chip"
            aria-pressed={selected === key}
            title={claims.get(key)?.text}
            onClick={() => setSelected(selected === key ? '' : key)}
          >
            {key}
          </button>
        ))}
      </li>
    );
  }

  const position = talks.findIndex(t => t.id === talkId);
  const available = talks.filter(t => t.summary_number !== null);
  const currentIndex = available.findIndex(t => t.id === talkId);
  const next = available.slice(currentIndex + 1).find(t => t.status !== 'reviewed');
  const lines = (kind: 'sentences' | 'conclusions') => summary[kind];
  return (
    <section aria-labelledby="summary-title" aria-busy={!!busy}>
      <p><button className="link" onClick={() => navigate('close')}><span aria-hidden="true">← </span>К списку докладов</button></p>
      {onNavigate && <nav className="row" aria-label="Доклады">
        <button disabled={currentIndex <= 0 || !!busy} onClick={() => navigate(available[currentIndex - 1].id)}>Предыдущий</button>
        <span>Доклад {position + 1} из {talks.length}</span>
        <button disabled={currentIndex + 1 >= available.length || !!busy} onClick={() => navigate(available[currentIndex + 1].id)}>Следующий</button>
      </nav>}
      {pending && <div role="dialog" aria-label="Несохранённые правки" className="card">
        <p>Есть несохранённые правки.</p>
        <button onClick={() => void (async () => {
          try {
            await saveMetadata();
            if (draft && !await save()) return;
            leave(pending);
          } catch (e) { setError((e as Error).message); }
        })()}>Сохранить и перейти</button>
        <button onClick={() => { leave(pending); }}>Отменить правки и перейти</button>
        <button onClick={() => setPending(undefined)}>Остаться</button>
      </div>}
      <div className="card summary-header">
        <h2 id="summary-title">{metadata?.title ?? title ?? view.talk_title ?? 'Доклад без названия'}</h2>
      {metadata && <div className="talk-metadata">
        {metaDraft ? <>
          <CardEditor card={metaDraft} onChange={setMetaDraft} />
          <button onClick={() => void saveMetadata().catch(e => setError((e as Error).message))}>Сохранить карточку</button>
          <button onClick={() => setMetaDraft(undefined)}>Отменить</button>
        </> : <><CardView card={{ ...metadata, title: metadata.title ?? '' }} />
          <button className="link" onClick={() => setMetaDraft({ ...metadata, title: metadata.title ?? '' })}>Править карточку</button></>}
      </div>}

        <div className="kv">
          <div><b>Доклад</b><span className={`pill ${statusTone[talkStatus] ?? ''}`}>{talkStatuses[talkStatus] ?? talkStatus}</span></div>
          <div><b>Версия сводки</b>{summary.version}</div>
          <div><b>Автор версии</b>{summary.human_edit ? 'правка человека' : `модель, профиль ${summary.profile}`}</div>
          {summary.status === 'needs_attention' && (
            <div><b>Проверки</b><span className="pill warn">не пройдены, нужна правка</span></div>
          )}
        </div>
        <p role="status">{busy || status}</p>
        {error && <p role="alert">{error}</p>}
      </div>

      <div className="review">
        <div className="card">
          <h3>Сводка</h3>
          {draft
            ? <ol className="summary-text">{lineEditor('sentences', 'Предложение')}</ol>
            : lines('sentences').length
              ? <ol className="summary-text">{lines('sentences').map(lineView)}</ol>
              : <p className="muted">Текста сводки нет.</p>}
          <h3>Выводы</h3>
          {draft
            ? (draft.conclusions.length ? <ul className="summary-text">{lineEditor('conclusions', 'Вывод')}</ul> : <p className="muted">Выводов нет.</p>)
            : lines('conclusions').length
              ? <ul className="summary-text">{lines('conclusions').map(lineView)}</ul>
              : <p className="muted">Выводов нет.</p>}

          {draft && (
            <>
              <h3>Утверждения</h3>
              <p className="muted">Правка текста утверждения снимает с него подтверждение.</p>
              {summary.claims.filter((item) => used.has(item.key)).map((item) => (
                <label key={item.key} className="field" style={{ marginBottom: '.5rem' }}>
                  <span>{item.key} · {claimTypes[item.type] ?? item.type}</span>
                  <input
                    value={draft.claims[item.key]}
                    disabled={!!busy}
                    onChange={(event) => {
                      const text = event.target.value;
                      setDraft((previous) => previous && ({
                        ...previous, claims: { ...previous.claims, [item.key]: text },
                      }));
                    }}
                  />
                </label>
              ))}
            </>
          )}

          <div className="row" style={{ marginTop: '1rem' }}>
            {draft ? (
              <>
                <button className="primary" disabled={!!busy} onClick={save}>Сохранить правку</button>
                <button disabled={!!busy} onClick={() => setDraft(undefined)}>Отменить</button>
              </>
            ) : (
              <>
                <button
                  className="primary"
                  disabled={!!busy || !!metaDraft || talkStatus === 'reviewed' || !summary.sentences.length}
                  onClick={() => void act('Подтверждение…', 'Сводка проверена.', () => request(`${url}/review`, {
                    method: 'POST',
                    headers: json,
                    body: JSON.stringify({ version: summary.version }),
                  }))}
                >
                  Подтвердить проверку
                </button>
                {onNavigate && <button disabled={!!busy || !!metaDraft || talkStatus === 'reviewed' || !summary.sentences.length}
                  onClick={() => void (async () => {
                    const ok = await act('Подтверждение…', 'Сводка проверена.', () => request(`${url}/review`, {
                      method: 'POST', headers: json, body: JSON.stringify({ version: summary.version }),
                    }));
                    if (ok && next) onNavigate(next.id);
                  })()}>Подтвердить и следующий</button>}
                <button disabled={!!busy} onClick={startEdit}>Править</button>
                <button
                  disabled={!!busy}
                  onClick={() => void act('Обновление…', 'Загружена текущая версия.', () => request(url))}
                >
                  Обновить
                </button>
              </>
            )}
          </div>
        </div>

        {claim ? (
          <aside className="card claim-card" aria-label={`Утверждение ${claim.key}`}>
            <h3 style={{ marginTop: 0 }}>{claim.key} · {claimTypes[claim.type] ?? claim.type}</h3>
            <p>{claim.text}</p>
            <p><span className={`pill ${claimTone[claim.status]}`}>{claimStatuses[claim.status]}</span></p>
            {claim.evidence.map((item, index) => {
              const fragment = fragments.get(item.fragment_revision_id);
              return (
                <figure key={index}>
                  <figcaption>
                    {item.label}{fragment && ` · ${place(fragment)}`}
                  </figcaption>
                  {fragment && <blockquote>{highlight(fragment.text, item.span)}</blockquote>}
                  {fragment?.locator.kind === 'time' && <audio controls preload="none"
                    src={`/api/v1/files/${fragment.source_file_id}/content#t=${fragment.locator.start_ms / 1000},${fragment.locator.end_ms / 1000}`} />}
                  {fragment?.locator.kind === 'page' && (
                    <img
                      src={`/api/v1/files/${fragment.source_file_id}/${fragment.kind.startsWith('slide') ? 'slides' : 'pages'}/${fragment.locator.page_no}`}
                      alt={`Слайд ${fragment.locator.page_no}`}
                      loading="lazy"
                    />
                  )}
                </figure>
              );
            })}
            {claim.rejected_evidence.map((item, index) => (
              <p key={`rejected-${index}`}>
                <small>
                  Не подтверждено ({rejections[item.reason] ?? item.reason}): {item.label}
                  {byLabel.has(item.label) ? '' : ' (метки нет в пакете)'} — «{item.quote}»
                </small>
              </p>
            ))}
          </aside>
        ) : (
          <div className="card claim-card muted">
            Нажмите на ссылку утверждения рядом с предложением, например c1, чтобы увидеть
            цитату из источника и слайд.
          </div>
        )}
      </div>
      {conferenceId && <Recommendations talkId={talkId} />}
    </section>
  );
}

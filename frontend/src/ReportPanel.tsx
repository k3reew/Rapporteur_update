import { useEffect, useRef, useState } from 'react';

import type { ConferenceItem } from './conferenceTypes';
import { useResource } from './useResource';
import { request } from './api';
import { useUnsavedChanges } from './useUnsavedChanges';
import type { TalkProgress, Progress } from './talkTypes';

type ReportVersion = {
  id: string;
  number: number;
  created_at: string;
  files: { format: 'docx' | 'md' | 'pdf' }[];
  variables: { talks_count: number };
  warnings: string[];
};
type ReportJob = { id: string; status: 'queued' | 'running' | 'done' | 'failed' | 'paused'; step: string; error: string | null; report_version_id: string | null };
type ReportContact = { organization: string; position: string; person: string; talk: string; cooperation: string };
type ReportFields = {
  participation: string;
  program: string;
  overview: string;
  attachments: string;
  contacts: ReportContact[] | null;
};
type ReportEdit = { revision: number; fields: ReportFields };
type PreviewTalk = {
  id: string;
  speakers: { name: string; position: string | null; affiliation: string | null; contacts: string[] }[];
  organizations: string[];
  contacts: string[];
  annotation: string[];
  conclusions: string[];
};
type ReportDraft = ReportEdit & { suggested_contacts: ReportContact[]; talks: PreviewTalk[] };
const fieldLabels = {
  participation: 'Участие организации',
  program: 'Программа конференции',
  overview: 'Общие сведения',
  attachments: 'Приложения',
} as const;
const contactLabels = {
  organization: 'Организация', position: 'Должность', person: 'ФИО / телефон',
  talk: 'Доклад', cooperation: 'Совместная работа',
} as const;
const blankContact: ReportContact = { organization: '', position: '', person: '', talk: '', cooperation: '' };
const jobSteps: Record<string, string> = { queued: 'В очереди', running: 'Подготовка', docx: 'Документ DOCX', md: 'Текст Markdown', pdf: 'Документ PDF', done: 'Готово', failed: 'Ошибка', paused: 'ждёт команды оператора' };
const json = { 'Content-Type': 'application/json' };
const formats: Record<string, string> = { docx: 'DOCX', md: 'Markdown', pdf: 'PDF' };

/** Доклад с актуальным отбором: готовая сводка у интересного отделам или карточка у остальных. */
function canReport(talk: TalkProgress) {
  return !!talk.reportable && (!!talk.card_only || talk.summary_number !== null);
}

function reportBlocker(talk: TalkProgress) {
  if (talk.screening?.error || talk.job === 'failed') return 'Повторите обработку или проверьте материалы';
  if (talk.screening?.stale || talk.summary_status === 'stale') return 'Обновите отбор или сводку';
  if (talk.job === 'paused' || talk.screening?.state === 'paused') return 'Продолжите обработку';
  if (talk.job === 'queued' || talk.job === 'running' || ['queued', 'running'].includes(talk.screening?.state ?? '')) return 'Идёт анализ';
  if (talk.screening && !talk.screening.manual) return 'Подтвердите решение по отделам';
  if (talk.summary_number !== null) return 'Проверьте сводку';
  return 'Проверьте наличие материалов и запустите анализ';
}

export function ReportPanel({ conferenceId }: { conferenceId: string }) {
  const base = `/api/v1/conferences/${conferenceId}`;
  const { data: jobs = [], error: jobsError, refresh: refreshJobs } = useResource<ReportJob[]>(`${base}/report-jobs`, 2000);
  const pending = jobs.some(job => job.status === 'queued' || job.status === 'running');
  const submission = useRef({ body: '', key: '', jobId: '' });
  const completed = jobs.filter(job => job.status === 'done').map(job => job.report_version_id).join(',');
  const [conference, setConference] = useState<ConferenceItem>();
  const [dates, setDates] = useState({ start: '', end: '', place: '', organizer: '' });
  const { data: progress, error: progressError } = useResource<Progress>(`${base}/progress`, 5000);
  const operator = progress?.workflow === 'operator';
  const { data: draft, error: draftError, setData: setDraft } = useResource<ReportDraft>(operator ? `${base}/report-draft` : null, 5000);
  const [edit, setEdit] = useState<ReportEdit | null>(null);
  const fields = edit?.fields ?? draft?.fields;
  const contactRows = fields?.contacts ?? draft?.suggested_contacts ?? [];
  const talks = (progress?.talks ?? []).filter(talk => !operator || !['not_held', 'insufficient_data'].includes(talk.status));
  const [excluded, setExcluded] = useState<ReadonlySet<string>>(new Set());
  const selected = talks.filter(talk => operator || (canReport(talk) && !excluded.has(talk.id))).map(talk => talk.id);
  const blocked = talks.some(talk => !canReport(talk));
  const [versions, setVersions] = useState<ReportVersion[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState('');
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');

  const datesDirty = !!conference && (dates.start !== (conference.start_date ?? '') || dates.end !== (conference.end_date ?? '') || dates.place !== (conference.place ?? '') || dates.organizer !== (conference.organizer ?? ''));
  useUnsavedChanges(datesDirty || !!edit || !!busy);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      const signal = controller.signal;
      const value: ConferenceItem = await (await request(base, { signal })).json();
      if (signal.aborted) return;
      setConference(value);
      setDates({ start: value.start_date ?? '', end: value.end_date ?? '', place: value.place ?? '', organizer: value.organizer ?? '' });
      const history = await (await request(`${base}/reports`, { signal })).json();
      if (!signal.aborted) setVersions(history);
    }
    void load().catch((reason: Error) => {
      if (!controller.signal.aborted) setError(reason.message);
    });
    return () => controller.abort();
  }, [base]);

  useEffect(() => {
    if (!completed) return;
    const controller = new AbortController();
    void request(`${base}/reports`, { signal: controller.signal }).then(response => response.json()).then((history: ReportVersion[]) => {
      if (!controller.signal.aborted) setVersions(history);
    }).catch((reason: Error) => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [base, completed]);

  async function run(label: string, action: () => Promise<void>) {
    setBusy(label);
    setError('');
    setStatus('');
    try {
      await action();
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy('');
    }
  }

  function saveDates() {
    if (!conference) return;
    void run('Сохранение…', async () => {
      const response = await request(base, {
        method: 'PUT',
        headers: json,
        body: JSON.stringify({
          title: conference.title,
          start_date: dates.start || null,
          end_date: dates.end || dates.start || null,
          place: dates.place.trim() || null,
          organizer: dates.organizer.trim() || null,
        }),
      });
      const saved: ConferenceItem = await response.json();
      setConference(saved);
      setDates({ start: saved.start_date ?? '', end: saved.end_date ?? '', place: saved.place ?? '', organizer: saved.organizer ?? '' });
      setStatus('Сведения о конференции сохранены.');
    });
  }

  function editFields(value: ReportFields) {
    if (!draft) return;
    setEdit(previous => ({ revision: previous?.revision ?? draft.revision, fields: value }));
  }

  function saveFields() {
    if (!edit) return;
    void run('Сохранение…', async () => {
      const response = await request(`${base}/report-draft`, {
        method: 'PUT', headers: json, body: JSON.stringify(edit),
      });
      setDraft(await response.json() as ReportDraft);
      setEdit(null);
      setStatus('Черновик справки сохранён.');
    });
  }

  function build() {
    const body = JSON.stringify({
      unreviewed_confirmed: !operator && !progress?.auto_start && confirmed,
      talk_ids: selected,
      ...(operator ? { draft_revision: draft?.revision } : {}),
    });
    void run('Постановка в очередь…', async () => {
      if (submission.current.body !== body || jobs.some(job => job.id === submission.current.jobId && ['done', 'failed'].includes(job.status))) {
        // getRandomValues works over LAN HTTP, unlike randomUUID (secure contexts only).
        const key = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
        submission.current = { body, key, jobId: '' };
      }
      const response = await request(`${base}/report-jobs`, { method: 'POST', headers: json,
        body: JSON.stringify({ ...JSON.parse(body), idempotency_key: submission.current.key }),
      });
      submission.current.jobId = ((await response.json()) as ReportJob).id;
      setStatus('Сборка поставлена в очередь. Можно переходить к другим разделам.');
      refreshJobs();
    });
  }

  function retry(job: ReportJob) {
    void run('Повтор сборки…', async () => {
      await request(`/api/v1/report-jobs/${job.id}/retry`, { method: 'POST' });
      refreshJobs();
    });
  }

  return (
    <section aria-labelledby="report-title" aria-busy={!!busy}>
      <div className="card">
        <h2 id="report-title">{operator ? 'Предпросмотр справки' : 'Справка'}</h2>
        <p role="status">{busy && <span className="spinner" aria-hidden="true" />}{busy || status}</p>
        {(error || jobsError || progressError || draftError) && <p role="alert">{error || jobsError || progressError || draftError}</p>}
        {conference && (
          <form onSubmit={(event) => { event.preventDefault(); saveDates(); }}>
            <fieldset>
              <legend>Сведения для справки</legend>
              <div className="fields">
                <label className="field"><span>Начало</span>
                  <input type="date" value={dates.start} onChange={(event) => setDates({ ...dates, start: event.target.value })} />
                </label>
                <label className="field"><span>Окончание</span>
                  <input type="date" value={dates.end} onChange={(event) => setDates({ ...dates, end: event.target.value })} />
                </label>
                <label className="field"><span>Место</span>
                  <input value={dates.place} placeholder="г. Москва" onChange={(event) => setDates({ ...dates, place: event.target.value })} />
                </label>
                {operator && <label className="field"><span>Организатор</span>
                  <input value={dates.organizer} onChange={(event) => setDates({ ...dates, organizer: event.target.value })} />
                </label>}
                <button disabled={!!busy} style={{ alignSelf: 'end' }}>Сохранить</button>
              </div>
              {!operator && !conference.start_date && (
                <p className="pill warn" style={{ marginTop: '.8rem' }}>
                  Без дат в справке не будет периода проведения, сборка не пройдёт.
                </p>
              )}
            </fieldset>
          </form>
        )}
      </div>
      {operator && fields && <form className="card" onSubmit={event => { event.preventDefault(); saveFields(); }}>
        <fieldset disabled={!!busy}>
          <legend>Поля бланка</legend>
          <p className="muted">Пустые поля можно заполнить вручную после выгрузки.</p>
          <div className="fields">
            {(Object.keys(fieldLabels) as (keyof typeof fieldLabels)[]).map(key => <label className="field" key={key}>
              <span>{fieldLabels[key]}</span>
              <textarea rows={3} maxLength={key === 'program' || key === 'overview' ? 8000 : 4000}
                placeholder={key === 'program' ? 'Секции из реестра' : undefined}
                value={fields[key]} onChange={event => editFields({ ...fields, [key]: event.target.value })} />
            </label>)}
          </div>
          <h3>Контакты и возможная совместная работа</h3>
          {contactRows.map((row, index) => <fieldset className="fields" key={index}>
            <legend>Контакт {index + 1}</legend>
            {(Object.keys(contactLabels) as (keyof ReportContact)[]).map(key => <label className="field" key={key}>
              <span>{contactLabels[key]}</span>
              <input value={row[key]} maxLength={key === 'cooperation' ? 2000 : 1000}
                onChange={event => editFields({ ...fields, contacts: contactRows.map((item, i) => i === index ? { ...item, [key]: event.target.value } : item) })} />
            </label>)}
            <button type="button" onClick={() => editFields({ ...fields, contacts: contactRows.filter((_, i) => i !== index) })}>Удалить контакт</button>
          </fieldset>)}
          <div className="row">
            <button type="button" disabled={contactRows.length >= 500} onClick={() => editFields({ ...fields, contacts: [...contactRows, blankContact] })}>Добавить контакт</button>
            <button disabled={!edit}>Сохранить поля</button>
            {edit && <button type="button" onClick={() => setEdit(null)}>Отменить правки</button>}
          </div>
        </fieldset>
      </form>}
      <div className="card">
        <h3 style={{ marginTop: 0 }}>{operator ? 'Доклады в справке' : 'Сборка'}</h3>
        {!operator && <p className="muted">
          Выберите доклады для справки. Карточки содержат название и авторов. Полные сводки также содержат ключевые тезисы и выводы, если они есть в источниках.
        </p>}
        {progress && !talks.length && <p className="notice">Сначала <a href={`#/c/${encodeURIComponent(conferenceId)}/materials`}>загрузите материалы и начните анализ</a>.</p>}
        {blocked && <p className="notice">Часть докладов пока недоступна для выпуска. <a href={`#/c/${encodeURIComponent(conferenceId)}/review`}>Перейти к проверке</a>.</p>}
        {operator ? talks.map(talk => {
          const preview = draft?.talks.find(item => item.id === talk.id);
          const people = preview?.speakers.map(person => [person.name, person.position, person.affiliation, ...person.contacts].filter(Boolean).join(', '));
          const details = [...(people ?? []), ...(preview?.organizations ?? []), ...(preview?.contacts ?? [])];
          return <details key={talk.id} className="report-talk-preview">
            <summary>{talk.title || 'Без названия'} · {talk.section}</summary>
            {details.length ? <p>{details.join(' · ')}</p> : <p><a href={`#/c/${encodeURIComponent(conferenceId)}/talks`}>Заполнить реквизиты в реестре</a></p>}
            {!canReport(talk) && <p className="muted">{reportBlocker(talk)}</p>}
            {talk.card_only && <p className="muted">Карточка</p>}
            {!!preview?.annotation.length && <p>{preview.annotation.join(' ')}</p>}
            {!!preview?.conclusions.length && <ul>{preview.conclusions.map((line, index) => <li key={index}>{line}</li>)}</ul>}
            {talk.summary_number !== null && !talk.card_only && <a href={`#/c/${encodeURIComponent(conferenceId)}/talk/${encodeURIComponent(talk.id)}`}>Проверить или исправить аннотацию</a>}
          </details>;
        }) : !!talks.length && <fieldset disabled={!!busy} className="report-selection"><legend>Доклады — выбрано {selected.length} из {talks.length}</legend>
          {talks.map(talk => <label className="check" key={talk.id}>
            <input type="checkbox" aria-label={talk.title ?? 'Без темы'} disabled={!canReport(talk)} checked={selected.includes(talk.id)}
              onChange={event => {
                const checked = event.target.checked;
                setExcluded(previous => {
                  const next = new Set(previous);
                  if (checked) next.delete(talk.id);
                  else next.add(talk.id);
                  return next;
                });
              }} />
            <span>{talk.title ?? 'Без темы'}{!canReport(talk)
              ? <small className="talk-subline">{reportBlocker(talk)}</small>
              : <small> · {talk.card_only ? 'только карточка' : talk.status === 'reviewed' ? 'полная сводка' : 'ожидает проверки'}</small>}</span>
          </label>)}
        </fieldset>}
        {(datesDirty || edit) && <p className="notice">Сохраните правки перед выпуском.</p>}
        <div className="row">
          <button className="primary" disabled={!!busy || pending || !selected.length || datesDirty || !!edit || (operator ? !draft || blocked || !progress?.screening_confirmed : progress?.auto_start && !conference?.start_date)} onClick={build}>{operator ? 'Выпустить справку' : 'Собрать справку'}</button>
          {progress && !operator && !progress.auto_start && <label className="check">
            <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
            Собрать, даже если не все сводки проверены
          </label>}
        </div>
        {!!jobs.length && <section aria-label="Задания выпуска"><h3>Задания выпуска</h3>
          {jobs.map(job => <details key={job.id} open={job.status === 'failed' || job.status === 'paused'}><summary>{job.status === 'done' ? 'Справка собрана' : job.status === 'failed' ? 'Сборка не завершена' : job.status === 'paused' ? 'Сборка приостановлена после перезапуска' : 'Справка собирается'} · {jobSteps[job.step] ?? job.step}</summary>
            <p className="muted">Задание {job.id}</p>{job.error && <p role="alert">{job.error}</p>}
            {job.status === 'failed' && <button disabled={!!busy} onClick={() => retry(job)}>Повторить сборку</button>}
            {job.status === 'paused' && <button disabled={!!busy} onClick={() => retry(job)}>Продолжить сборку</button>}
          </details>)}
        </section>}
        <h3>Версии</h3>
        {versions.length > 0 ? (
          <ul className="versions">
            {versions.map((version) => (
              <li key={version.id}>
                <b>Версия {version.number}</b>{' '}
                <span className="muted">
                  от {new Date(version.created_at).toLocaleString('ru-RU')}, докладов {version.variables.talks_count}
                </span>{' '}
                <span className="files">
                  {version.files.map((file) => (
                    <a key={file.format} className="pill info" href={`/api/v1/report-versions/${version.id}/files/${file.format}`}>
                      {formats[file.format]}
                    </a>
                  ))}
                </span>
                {!!version.warnings.length && <ul className="muted">{version.warnings.map(item => <li key={item}>{item}</li>)}</ul>}
              </li>
            ))}
          </ul>
        ) : <p className="muted">Собранных версий пока нет.</p>}
      </div>
    </section>
  );
}

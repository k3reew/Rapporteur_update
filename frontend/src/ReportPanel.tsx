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
const jobSteps: Record<string, string> = { queued: 'В очереди', running: 'Подготовка', docx: 'Документ DOCX', md: 'Текст Markdown', pdf: 'Документ PDF', done: 'Готово', failed: 'Ошибка', paused: 'ждёт команды оператора' };
const json = { 'Content-Type': 'application/json' };
const formats: Record<string, string> = { docx: 'DOCX', md: 'Markdown', pdf: 'PDF' };

/** Доклад с актуальным отбором: готовая сводка у интересного отделам или карточка у остальных. */
function canReport(talk: TalkProgress) {
  return !!talk.reportable && (!!talk.card_only || talk.summary_number !== null);
}

export function ReportPanel({ conferenceId }: { conferenceId: string }) {
  const base = `/api/v1/conferences/${conferenceId}`;
  const { data: jobs = [], error: jobsError, refresh: refreshJobs } = useResource<ReportJob[]>(`${base}/report-jobs`, 2000);
  const pending = jobs.some(job => job.status === 'queued' || job.status === 'running');
  const submission = useRef({ body: '', key: '', jobId: '' });
  const completed = jobs.filter(job => job.status === 'done').map(job => job.report_version_id).join(',');
  const [conference, setConference] = useState<ConferenceItem>();
  const [dates, setDates] = useState({ start: '', end: '', place: '' });
  const [talks, setTalks] = useState<TalkProgress[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [versions, setVersions] = useState<ReportVersion[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState('');
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');

  const datesDirty = !!conference && (dates.start !== (conference.start_date ?? '') || dates.end !== (conference.end_date ?? '') || dates.place !== (conference.place ?? ''));
  useUnsavedChanges(datesDirty || !!busy);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      const signal = controller.signal;
      const value: ConferenceItem = await (await request(base, { signal })).json();
      if (signal.aborted) return;
      setConference(value);
      setDates({ start: value.start_date ?? '', end: value.end_date ?? '', place: value.place ?? '' });
      const progress: Progress = await (await request(`${base}/progress`, { signal })).json();
      if (signal.aborted) return;
      setTalks(progress.talks);
      setSelected(progress.talks.filter(canReport).map(talk => talk.id));
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
          organizer: conference.organizer,
        }),
      });
      const saved: ConferenceItem = await response.json();
      setConference(saved);
      setDates({ start: saved.start_date ?? '', end: saved.end_date ?? '', place: saved.place ?? '' });
      setStatus('Сведения о конференции сохранены.');
    });
  }

  function build() {
    const body = JSON.stringify({ unreviewed_confirmed: confirmed, talk_ids: selected });
    void run('Постановка в очередь…', async () => {
      if (submission.current.body !== body || jobs.some(job => job.id === submission.current.jobId && job.status === 'done')) {
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
        <h2 id="report-title">Справка</h2>
        <p role="status">{busy && <span className="spinner" aria-hidden="true" />}{busy || status}</p>
        {(error || jobsError) && <p role="alert">{error || jobsError}</p>}
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
                <button disabled={!!busy} style={{ alignSelf: 'end' }}>Сохранить</button>
              </div>
              {!conference.start_date && (
                <p className="pill warn" style={{ marginTop: '.8rem' }}>
                  Без дат в справке не будет периода проведения, сборка не пройдёт.
                </p>
              )}
            </fieldset>
          </form>
        )}
      </div>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Сборка</h3>
        <p className="muted">
          Выберите доклады для справки. Доклады без интереса отделов входят карточкой: тема, авторы, организации и контакты. Остальные доклады без готовой сводки в неё не войдут. Подтверждённые рекомендации отделам включаются вместе с основаниями.
        </p>
        <fieldset disabled={!!busy} className="report-selection"><legend>Доклады — выбрано {selected.length} из {talks.length}</legend>
          {talks.map(talk => <label className="check" key={talk.id}><input type="checkbox" aria-label={talk.title ?? 'Без темы'} disabled={!canReport(talk)} checked={selected.includes(talk.id)}
            onChange={event => setSelected(previous => event.target.checked ? [...previous, talk.id] : previous.filter(id => id !== talk.id))} />
            <span>{talk.title ?? 'Без темы'} <small>· {talk.card_only ? 'только карточка' : talk.status === 'reviewed' ? 'проверен' : talk.summary_status === 'stale' ? 'сводка устарела' : talk.summary_number ? 'ожидает проверки' : 'нет сводки'}</small></span></label>)}
        </fieldset>
        {datesDirty && <p className="notice">Сохраните сведения о конференции перед сборкой.</p>}
        <div className="row">
          <button className="primary" disabled={!!busy || pending || !selected.length || datesDirty} onClick={build}>Собрать справку</button>
          <label className="check">
            <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
            Собрать, даже если не все сводки проверены
          </label>
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

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { ConferenceScreening } from './ConferenceScreening';
import type { Screening, TalkProgress } from './talkTypes';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const item = (id: string, status: string) => ({
  department_id: id, department_name: id, status, interest: 76, reason: 'Обоснование',
  evidence: [{ label: 'p2', quote: 'Доказательство из доклада', source_url: '/api/v1/files/f/pages/2' }],
  paragraphs: [{ label: 'r1', quote: 'Функция отдела' }],
});
const screening = (items: Screening['items'], departmentIds: string[]): Screening => ({
  version: 2, state: 'relevant', eligible: false, stale: false, manual: false, error: null, items, department_ids: departmentIds,
});
const talk = (id: string, value: Screening): TalkProgress => ({
  id, title: id, section: '', order: 1, status: 'draft', step: 'screening', job: null, error: null,
  summary_number: null, summary_status: null, screening: value,
});

test('спорный отдел требует решения до отправки всего отбора', async () => {
  const start = vi.fn();
  const talks = [
    talk('Первый доклад', screening([item('Отдел А', 'relevant')], ['Отдел А'])),
    talk('Второй доклад', screening([item('Отдел Б', 'uncertain')], [])),
  ];
  const api = vi.fn(async (url: string) => Response.json(url.endsWith('/progress')
    ? { workflow: 'operator', processing_started: true, talks } : { files: [] }));
  vi.stubGlobal('fetch', api);
  render(<ConferenceScreening conferenceId="c" onConfirmed={start} />);
  const submit = await screen.findByRole<HTMLButtonElement>('button', { name: 'Подтвердить решения' });
  expect(submit.disabled).toBe(true);
  expect(screen.getAllByRole('link', { name: 'Источник доклада p2', hidden: true })[0].getAttribute('href')).toBe('/api/v1/files/f/pages/2');
  const choices = screen.getAllByRole('button', { name: 'Оставить карточку' });
  fireEvent.click(choices[1]);
  expect(submit.disabled).toBe(false);
  fireEvent.click(submit);
  await waitFor(() => expect(start).toHaveBeenCalled());
  expect(api).toHaveBeenCalledWith('/api/v1/conferences/c/screening', expect.objectContaining({
    method: 'PUT', body: JSON.stringify({ talks: [
      { talk_id: 'Первый доклад', version: 2, department_ids: ['Отдел А'] },
      { talk_id: 'Второй доклад', version: 2, department_ids: [] },
    ] }),
  }));
});

test('подтверждённое назначение можно заменить карточкой', async () => {
  const assigned = { ...screening([item('Отдел А', 'relevant')], ['Отдел А']), manual: true };
  const api = vi.fn(async (url: string) => Response.json(url.endsWith('/progress')
    ? { workflow: 'operator', processing_started: true, screening_confirmed: true, talks: [talk('Доклад', assigned)] }
    : { files: [] }));
  vi.stubGlobal('fetch', api);
  render(<ConferenceScreening conferenceId="c" onConfirmed={vi.fn()} />);
  fireEvent.click(await screen.findByText('Изменить решение'));
  fireEvent.click(screen.getByRole('button', { name: 'Оставить карточку' }));
  fireEvent.click(screen.getByRole('button', { name: 'Подтвердить решения' }));
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/v1/conferences/c/screening', expect.objectContaining({
    method: 'PUT', body: JSON.stringify({ talks: [{ talk_id: 'Доклад', version: 2, department_ids: [] }] }),
  })));
});

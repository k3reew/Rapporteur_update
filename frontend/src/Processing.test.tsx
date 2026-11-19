import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import { Processing } from './Processing';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const talk = {
  id: 't1', section_id: 's', section: 'Секция', order: 1, status: 'processed', job: 'done', step: null,
  error: null, summary_number: null, summary_status: null, bulk_reviewable: false,
};
/** manual — решение подтверждено оператором; без него обработка ждёт оператора. */
const screening = (state: string, ids: string[], manual = true) => ({
  version: 1, state, eligible: state === 'relevant' && manual, stale: false, manual, error: null, department_ids: ids,
  items: [{ department_id: 'd', department_name: 'Отдел', status: state, reason: 'Основание', evidence: [], paragraphs: [] }],
});

test('доклад без интереса отделов получает только карточку, оператор может это решить сам', async () => {
  const puts: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'PUT') {
      puts.push([url, JSON.parse(String(init.body))]);
      return Response.json({});
    }
    return Response.json({
      processing_started: true,
      files: 2,
      talks: [
        { ...talk, title: 'Кэширование', card_only: true, screening: screening('not_relevant', []) },
        { ...talk, id: 't2', order: 2, title: 'Сжатие', card_only: false, summary_number: 1, screening: screening('relevant', ['d']) },
      ],
    });
  }));
  render(<Processing conferenceId="c" onOpen={() => {}} />);

  expect(await screen.findByText('только карточка')).toBeDefined();
  const cardRow = screen.getByRole('rowheader', { name: /Кэширование/ }).closest('tr')!;
  expect(within(cardRow).getByText('Карточка готова')).toBeDefined();
  expect(within(cardRow).queryByText(/Сводка готова/)).toBeNull();
  expect(screen.getByText(/только карточка — у 1/)).toBeDefined();
  expect(screen.getByText('Интерес не найден — только карточка')).toBeDefined();
  const buttons = screen.getAllByRole<HTMLButtonElement>('button', { name: 'Открыть сводку' });
  expect(buttons.map(button => button.disabled)).toEqual([true, false]);

  fireEvent.click(screen.getByText('Есть интерес отделов'));
  // У каждого доклада своя кнопка, вторая — у «Сжатия».
  fireEvent.click(screen.getAllByRole('button', { name: 'Только карточка' })[1]);
  await vi.waitFor(() => expect(puts).toEqual([['/api/v1/talks/t2/screening', { version: 1, department_ids: [] }]]));
});

test('решение модели без подтверждения оператора отмечено и считается', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({
    processing_started: true,
    files: 1,
    talks: [{ ...talk, title: 'Кэширование', status: 'matched', job: null, step: 'screening', screening: screening('relevant', ['d'], false) }],
  })));
  render(<Processing conferenceId="c" onOpen={() => {}} />);

  expect(await screen.findByText('ждёт решения оператора')).toBeDefined();
  expect(screen.getByText('Ждут решения по отбору')).toBeDefined();
  expect(screen.getByText('Модель: есть интерес — подтвердите решение')).toBeDefined();
  expect(screen.getByRole('button', { name: 'Подтвердить интерес' })).toBeDefined();
});

// Цвет статуса доклада: зелёный — проверен, жёлтый — ждёт человека, синий — в работе.
export const statusTone: Record<string, string> = {
  matched: 'info',
  processed: 'warn',
  reviewed: 'ok',
  stale: 'warn',
  insufficient_data: 'bad',
};
export const materialStatuses: Record<string, string> = {
  draft: 'Черновик',
  matched: 'Материалы привязаны',
  insufficient_data: 'Недостаточно материалов',
  processed: 'Обработан',
  reviewed: 'Проверен',
  stale: 'Требуется обновление',
  not_held: 'Не состоялся',
};


export const talkStatuses: Record<string, string> = {
  draft: 'Черновик',
  matched: 'Материалы привязаны',
  insufficient_data: 'Недостаточно материалов',
  processed: 'Сводка готова, не проверена',
  reviewed: 'Проверен',
  stale: 'Требуется обновление',
  not_held: 'Не состоялся',
};

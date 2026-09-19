export interface RecommendationScenario {
  id: string;
  title: string;
  effect: string;
  risk: "Низкий" | "Средний" | "Высокий";
  cost: string;
  confidence: number;
}

export const pipelineStages = [
  "Данные очищены и приведены к стандарту",
  "Мониторинг входных показателей завершён",
  "Оркестратор распределил задачи агентам",
  "Сформировано 10 сценариев",
  "Отобрано 6 сценариев",
  "Подготовлен текст рекомендации",
];

export const recommendationScenarios: RecommendationScenario[] = [
  {
    id: "s1",
    title: "Стабилизация температуры T6",
    effect: "−1,8 мг/кг серы",
    risk: "Низкий",
    cost: "+2%",
    confidence: 92,
  },
  {
    id: "s2",
    title: "Снижение расхода F9",
    effect: "−1,2 мг/кг серы",
    risk: "Средний",
    cost: "−3%",
    confidence: 88,
  },
  {
    id: "s3",
    title: "Температура и расход · T6 + F9",
    effect: "−2,6 мг/кг серы",
    risk: "Средний",
    cost: "+1%",
    confidence: 84,
  },
  {
    id: "s4",
    title: "Коррекция давления P13",
    effect: "−0,9 мг/кг серы",
    risk: "Низкий",
    cost: "+1%",
    confidence: 81,
  },
  {
    id: "s5",
    title: "Смесь из двух резервуаров",
    effect: "T95 −4 °C",
    risk: "Средний",
    cost: "−5%",
    confidence: 79,
  },
  {
    id: "s6",
    title: "Смесь с присадкой",
    effect: "Цетан +1,4",
    risk: "Высокий",
    cost: "+7%",
    confidence: 73,
  },
];



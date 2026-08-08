"""Blind held-out benchmark D for the transaction categoriser.

Written WITHOUT reading `classifier.py`, `test_holdout.py`, `test_classifier.py` or
`make_ledger.py` — only `engine.py`'s category constants and the README were consulted, so
none of the vocabulary here is copied from the implementation under test. Narrations imitate
what a Kazakhstani 1C / bank statement export actually emits: invoice and contract numbers,
КБК codes, RU/KZ/EN mixed, abbreviations (НДС, КПН, ОПВ, ОСМС, ТБО, ГСМ, СМР, КС-2, ОГПО ВТС),
inconsistent capitalisation, one truncated line.

Labelling rules (standard accounting as used for loan-covenant testing):
  revenue    customer receipts, incl. prepayments and settlement of trade receivables
  opex       recurring operating costs: repairs & maintenance, consumables, fuel,
             third-party services, short-term licences, waste removal
  capex      acquisition / construction / reconstruction / modernisation of long-lived assets,
             incl. advances against them
  lease      rent and leasing (operating and finance lease instalments)
  payroll    wages, bonuses, termination compensation, and employee-related statutory
             contributions (ОПВ, ОСМС, СО) — even though these are remitted to a state body
  utilities  electricity, heat, water, gas — supply AND transmission/distribution
  tax        payments to the state budget (КПН, НДС, land/property tax, customs, emission
             charges, penalties on taxes)
  interest   the cost of borrowed money — in KZ contracts usually «вознаграждение»/«сыйақы»
  insurance  insurance premiums, whatever the insured risk is
  financing  loan principal: drawdowns (inflow) and repayments (outflow)

Deliberate traps, all resolvable by the rules above: «вознаграждение» meaning interest and not
a bonus; ОСМС being an insurance-named, budget-remitted payroll cost; electrical installation
of a new substation being capex and not utilities; a boiler-house reconstruction being capex
and not utilities; a repair booked on a КС-2 act being opex while construction on the same form
is capex; ТБО being opex and not utilities; an inflow described as «погашение» being revenue.

Anything genuinely arguable between two categories was left out on purpose — ИПН withheld at
source, социальный налог, per-diem reimbursements, lease-vs-utilities recharges by a landlord
and finance-lease principal splits are all absent because their label is contestable.

Shape: (description, counterparty, expected_category, is_inflow).
"""

# Blind benchmark: written without reading the classifier under test.
HELDOUT_D = [
    # (description, counterparty, expected_category, is_inflow)

    # ---- revenue ----
    ("Оплата по счету-фактуре № 2145 от 04.07.2025 за отгруженную продукцию, в т.ч. НДС 12%",
     "ТОО «Астана Пласт Трейд»", "revenue", True),
    ("Погашение дебиторской задолженности по акту сверки от 30.06.2025, дог. поставки №17/П",
     "АО «Кокшетау Минералды Сулары»", "revenue", True),
    ("PAYMENT FOR INVOICE INV-2025-0417, GRAIN HANDLING SERVICES JUNE 2025",
     "Solaris Commodities DMCC", "revenue", True),
    ("Тауар үшін төлем, шот-фактура №884, 07.2025 ж.",
     "ЖШС «Жетісу Агро Өнім»", "revenue", True),
    ("предоплата 50% по дог. №44/2025 за поставку арматуры А500С",
     "ТОО «СтройГрад Астана»", "revenue", True),
    ("ОПЛАТА ЗА УСЛУГИ ХРАНЕНИЯ ЗА ИЮНЬ 2025 Г. ПО ДОГОВОРУ №СХ-12/24 БЕЗ НДС",
     "ТОО «Каспий Логистик Групп»", "revenue", True),

    # ---- opex ----
    ("Оплата за ГСМ по топливным картам за июнь 2025 г., счет №77821",
     "ТОО «Гелиос Ойл Трейдинг»", "opex", False),
    ("Оплата за выполненные СМР по текущему ремонту кровли склада, акт КС-2 №3 от 28.06.25",
     "ТОО «Ремстройсервис KZ»", "opex", False),
    ("Услуги по вывозу ТБО за июль 2025 г. по дог. №112-Э",
     "ГКП на ПХВ «Тазалык-Актобе»", "opex", False),
    ("Оплата за услуги по подбору персонала по дог. №RS-08/25, этап 2",
     "ТОО «Астана Профи Партнерс»", "opex", False),
    ("Продление неисключительной лицензии 1С:Предприятие на 12 мес., счет №4471",
     "ТОО «Первый Бит Караганда»", "opex", False),
    ("Жанар-жағармай материалдары үшін төлем, шот №1129",
     "ЖШС «Ақжол Мұнай Сервис»", "opex", False),

    # ---- capex ----
    ("Авансовый платеж 30% за линию гранулирования по контракту №HZ-2025-07",
     "Henan Zhongke Machinery Co., Ltd", "capex", False),
    ("Оплата за СМР по строительству цеха №2, акт вып. работ КС-2 №7 от 30.06.2025",
     "ТОО «Промстрой Инжиниринг»", "capex", False),
    ("Оплата за электромонтажные работы по монтажу новой КТП-630 кВА на промплощадке",
     "ТОО «Энергомонтаж Актобе»", "capex", False),
    ("PREPAYMENT FOR EXCAVATOR CAT 320 GC, PROFORMA 55-A, DAP ALMATY",
     "Borusan Makina Kazakhstan LLP", "capex", False),
    ("Оплата за реконструкцию и модернизацию котельной, этап 1, дог. №РК-3/25",
     "ТОО «КазТеплоМонтаж»", "capex", False),
    ("Приобретение серверного оборудования Dell PowerEdge R750 (2 шт.), сч. №9912",
     "ТОО «Логиком Дистрибьюшн»", "capex", False),

    # ---- lease ----
    ("Арендная плата за офисное помещение (БЦ «Нурлы Тау», блок 4Б) за июль 2025",
     "ТОО «Нурлы Тау Проперти Менеджмент»", "lease", False),
    ("Лизинговый платеж №14 по договору финансового лизинга №ФЛ-221/23, седельный тягач",
     "АО «КазАгроФинанс»", "lease", False),
    ("Оплата за аренду складских помещений 1 200 кв.м, дог. №С-9/25, за июнь",
     "ТОО «Терминал Логистик Алатау»", "lease", False),
    ("Аренда земельного участка (промплощадка, 2,4 га) по дог. №ЗУ-7/24 за июль",
     "ТОО «Индустриальный парк Сарыарка»", "lease", False),
    ("OFFICE RENT JULY 2025 UNDER LEASE AGREEMENT 12-B, VAT INCLUDED",
     "Capital Partners Property LLP", "lease", False),
    ("Кеңсе үй-жайын жалдау ақысы, шілде 2025 ж., шарт №14-А",
     "ЖШС «Алатау Инвест Проперти»", "lease", False),

    # ---- payroll ----
    ("Зачисление на карточные счета работников по реестру №418 от 15.07.2025, 1-я пол. июля",
     "АО «Народный Банк Казахстана»", "payroll", False),
    ("Перечисление ОПВ за июнь 2025 г. согласно списку",
     "НАО «Государственная корпорация «Правительство для граждан»", "payroll", False),
    ("Отчисления и взносы на ОСМС за июнь 2025 г.",
     "НАО «Государственная корпорация «Правительство для граждан»", "payroll", False),
    ("Компенсация за неиспользованный трудовой отпуск при увольнении, приказ №44-к",
     "Сатыбалдиев Е.М.", "payroll", False),
    ("Выплата премии по итогам 2 квартала 2025 г. согласно приказу №61-п",
     "Абдрахманова Г.Т.", "payroll", False),
    ("Жалақы төлеу, 2025 ж. маусым айы, тізілім №77",
     "АО «Банк ЦентрКредит»", "payroll", False),

    # ---- utilities ----
    ("Оплата за электрическую энергию за июнь 2025 г., дог. ЭПО №4-118",
     "ТОО «Караганды Жарык»", "utilities", False),
    ("ОПЛАТА ЗА ТЕПЛОВУЮ ЭНЕРГИЮ ЗА ИЮНЬ 2025 Г. ПО ДОГ. №4/ТЭ ОТ 09.0",
     "АО «Астана-Теплотранзит»", "utilities", False),
    ("Холодное водоснабжение и водоотведение, июнь 2025, л/с 8827441",
     "ГКП на ПХВ «Су Арнасы»", "utilities", False),
    ("Оплата за природный газ, объем 12 400 м3, счет №GS-0771 за июнь",
     "АО «КазТрансГаз Аймак»", "utilities", False),
    ("Услуги по передаче и распределению электроэнергии за июнь 2025 г.",
     "АО «Мангистау Электросеть»", "utilities", False),
    ("Электр энергиясы үшін төлем, маусым 2025 ж., шарт №77-Э",
     "ЖШС «Батыс Энерго Транзит»", "utilities", False),

    # ---- tax ----
    ("Уплата КПН за 2 квартал 2025 г., КБК 101110",
     "УГД по Алмалинскому району г. Алматы", "tax", False),
    ("НДС за 2 квартал 2025 года, КБК 105101, декларация ф.300.00",
     "УГД по г. Актобе", "tax", False),
    ("Плата за эмиссии в окружающую среду за 2 кв. 2025 г.",
     "УГД по Мангистауской области", "tax", False),
    ("Таможенные платежи и НДС на импорт по ГТД №55408/230625/0004417",
     "Комитет государственных доходов МФ РК", "tax", False),
    ("Пеня по налогу на имущество юр. лиц за 2024 г., КБК 104102",
     "УГД по Костанайской области", "tax", False),
    ("Земельный налог за 2 квартал 2025 г.",
     "УГД по Есильскому району г. Астаны", "tax", False),

    # ---- interest ----
    ("Оплата вознаграждения по договору банковского займа №КЛ-118/24 за июль 2025",
     "АО «Банк ЦентрКредит»", "interest", False),
    ("Погашение начисленного вознаграждения по кредитной линии, график №7 от 10.07.25",
     "АО «Народный Банк Казахстана»", "interest", False),
    ("INTEREST PAYMENT UNDER FACILITY AGREEMENT DD 12.03.2024, PERIOD 01.04-30.06.25",
     "European Bank for Reconstruction and Development", "interest", False),
    ("Выплата купонного вознаграждения по облигациям KZ2C00009876, купон 4",
     "АО «Центральный депозитарий ценных бумаг»", "interest", False),
    ("Оплата % по договору займа №З-4/24 за 2 квартал 2025 г.",
     "ТОО «Сары-Арка Капитал»", "interest", False),
    ("Сыйақы төлеу, банктік қарыз шарты №BQ-19/24, шілде 2025 ж.",
     "АО «Bereke Bank»", "interest", False),

    # ---- insurance ----
    ("Страховая премия по договору страхования имущества, заложенного по дог. займа №КЛ-118/24",
     "АО «Евразия»", "insurance", False),
    ("Оплата по полису ОГПО ВТС №01-2255741, 12 ед. автотранспорта",
     "АО «Халык-Казахинстрах»", "insurance", False),
    ("Премия по договору страхования грузов (карго), полис KZ-2025-118",
     "АО «Нефтяная страховая компания»", "insurance", False),
    ("PREMIUM UNDER PROPERTY ALL RISKS POLICY PAR-2025-014, INSTALMENT 2/4",
     "AIG Kazakhstan JSC", "insurance", False),
    ("Страхование ГПО работодателя за причинение вреда работникам, полис №44-ГПО",
     "АО «Коммеск-Өмір»", "insurance", False),
    ("Оплата страховой премии по КАСКО, полис №АК-77213, рассрочка 2/4",
     "АО «Jusan Garant»", "insurance", False),

    # ---- financing ----
    ("Зачисление транша №3 по соглашению об открытии кредитной линии №КЛ-118/24",
     "АО «Банк ЦентрКредит»", "financing", True),
    ("Частичное досрочное погашение основного долга по банковскому займу №БЗ-77/23",
     "АО «Народный Банк Казахстана»", "financing", False),
    ("PRINCIPAL REPAYMENT, TRANCHE 3, LOAN AGREEMENT 45/22",
     "European Bank for Reconstruction and Development", "financing", False),
    ("Поступление по договору беспроцентного займа от участника ТОО, №ЗУ-2/25",
     "ТОО «Алтын Дала Холдинг»", "financing", True),
    ("Возврат основного долга по договору займа №З-4/24, график п.6",
     "ТОО «Сары-Арка Капитал»", "financing", False),
    ("Освоение кредитных средств по кред. дог. №ИП-9/25 (выборка транша)",
     "АО «Фонд развития промышленности»", "financing", True),
]

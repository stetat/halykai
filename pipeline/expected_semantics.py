# Independent reading of what each covenant requires, written WITHOUT seeing the engine.
#
# Source of truth: the Russian text of Статья 6 in each borrower's *current* contract
# (docmap.json -> current_contract), read in full via `pdftotext -raw`.  Статья 1 of every
# contract is boilerplate-identical and defines none of the financial terms used in Статья 6,
# so each clause is self-contained: every definition that matters is inside the clause itself.
#
# Formula vocabulary (categories):
#   revenue, opex, capex, lease, payroll, utilities, tax, interest, insurance,
#   financing, related_party, ebitda, other
# Operations: + - / ( ) and max(a, b, ...)
#
# `operator` / `threshold` are copied from covenant_specs.json.  Every one of the 36 was
# re-derived from the clause text and none contradicted the JSON.
#
# Conventions used below:
#   * «превышал X» / «не должны превышать X» / «более X»  -> compliant while value <= X
#   * «не менее X» / «не ниже X»                          -> compliant while value >= X
#   * Springing triggers, materiality floors, add-backs, period restrictions and
#     counterparty-scope filters are described in `note`, never encoded in `formula`.

EXPECTED = {
    # ------------------------------------------------------------------ P1 / ACC-7801
    # contract 904dea48b34b.txt (actually a PDF), Aktau Port Services JSC
    "P1": {
        "6.1": {
            "formula": "capex / (opex + lease)",
            "operator": "<=",
            "threshold": 0.42,
            "note": (
                "«Коэффициент капиталоёмкости означает отношение совокупных капитальных затрат "
                "за период к сумме операционных расходов и арендных платежей за тот же период.» "
                "Numerator = capex; denominator = opex + lease (arendnye platezhi = lease, a "
                "separate category from opex — the clause adds them, it does not treat lease as "
                "part of opex). Reclassification rule: «Суммы, переклассифицированные аудиторами "
                "Заёмщика, учитываются по переклассифицированной статье как в числителе, так и в "
                "знаменателе» — auditor reclassifications move the amount in BOTH numerator and "
                "denominator, i.e. apply reclass first, then compute."
            ),
        },
        "6.2": {
            "formula": "revenue",
            "operator": ">=",
            "threshold": 7100000.0,
            "note": (
                "«поддерживать совокупный объём таких поступлений ... на уровне не менее "
                "$7,100,000.00», where «поступлениями по статье «Выручка» понимаются суммы, "
                "отнесённые к данной статье в аудированной финансовой отчётности Заёмщика с "
                "учётом переквалификаций, произведённых аудиторами Заёмщика для целей соблюдения "
                "ковенантов» — post-reclassification revenue, both in and out."
            ),
        },
        "6.3": {
            "formula": "related_party",
            "operator": "<=",
            "threshold": 450000.0,
            "note": (
                "«совокупный объём платежей в пользу связанных сторон ... превышал $450,000.00». "
                "Absolute USD cap, not a ratio (contrast the sibling 6.3 clauses of P2/P4/P8/P10, "
                "which are ratios of revenue). Party scope: «Связанные стороны определяются в "
                "соответствии с МСФО (IAS) 24 и сведениями, раскрытыми в досье «Знай своего "
                "клиента» (KYC)» — KYC dossier decides, not the ledger description."
            ),
        },
    },
    # ------------------------------------------------------------------ P2 / ACC-7802
    # contract b43708a40daa.pdf, Almaty Cold Chain JSC
    "P2": {
        "6.1": {
            "formula": "(revenue + financing) / (opex + capex)",
            "operator": ">=",
            "threshold": 1.2,
            "note": (
                "«Отношение суммы выручки и поступлений по финансированию Заёмщика ... к сумме "
                "операционных и капитальных затрат ... должно составлять не менее 1.20x.» "
                "Sources = revenue + financing inflows; applications = opex + capex. Note this is "
                "the only 6.1 in the set with financing in the NUMERATOR (P3 puts financing in the "
                "numerator over EBITDA); here it is a sources/uses cover ratio."
            ),
        },
        "6.2": {
            "formula": "capex",
            "operator": "<=",
            "threshold": 3000000.0,
            "note": (
                "«такие совокупные расходы ... превысили $3,000,000.00», where «под расходами по "
                "статье «Капитальные затраты» понимаются суммы, отнесённые к данной статье ... "
                "включая суммы, переквалифицированные в неё независимым аудитором Заёмщика, и за "
                "вычетом сумм, переквалифицированных аудитором из данной статьи» — capex net of "
                "auditor reclassifications in BOTH directions (in and out)."
            ),
        },
        "6.3": {
            "formula": "related_party / revenue",
            "operator": "<=",
            "threshold": 0.03,
            "note": (
                "«совокупные Ограниченные платежи в пользу аффилированных лиц ... превышали 0.03x "
                "от выручки за тот же период». Ratio of revenue. Party scope: «Отнесение "
                "контрагента к аффилированным лицам определяется в соответствии с политикой в "
                "отношении связанных сторон, зафиксированной в досье Заёмщика по идентификации "
                "клиента, а не назначением платежа» — KYC dossier governs, payment purpose does not."
            ),
        },
    },
    # ------------------------------------------------------------------ P3 / ACC-7803
    # contract 89af6ae7964f.pdf, Shymkent Refinery Services JSC
    "P3": {
        "6.1": {
            "formula": "financing / ebitda",
            "operator": "<=",
            "threshold": 1.7,
            "note": (
                "SPRINGING covenant: «Ограничение отношения поступлений по финансированию к "
                "EBITDA величиной 1.70x ... применяется к Заёмщику ... ТОЛЬКО ПРИ УСЛОВИИ, что "
                "совокупные поступления по финансированию превышают $4,000,000.00.» The test is "
                "not applicable at all unless financing > $4,000,000; below that trigger the "
                "borrower cannot breach 6.1 whatever the ratio is. EBITDA is NOT defined anywhere "
                "in this contract (Статья 1 is boilerplate and silent); sibling contracts P5 and "
                "B1 define it as «Выручка за вычетом Операционных расходов», so revenue - opex is "
                "the natural fill-in. FX: «Суммы в иностранной валюте пересчитываются по курсу, "
                "раскрытому аудитором.»"
            ),
        },
        "6.2": {
            "formula": "revenue",
            "operator": ">=",
            "threshold": 6500000.0,
            "note": (
                "«совокупные поступления ... по указанной статье [«Выручка»] ... будут не ниже "
                "$6,500,000.00». One-directional reclass rule: «Суммы, переквалифицированные "
                "независимым аудитором Заёмщика в состав финансовых или иных неоперационных "
                "статей, в счёт исполнения настоящего ковенанта не засчитываются независимо от их "
                "первоначального отражения в учёте» — amounts reclassified OUT of revenue into "
                "financial/non-operating lines are dropped."
            ),
        },
        "6.3": {
            "formula": "related_party",
            "operator": "<=",
            "threshold": 400000.0,
            "note": (
                "«совокупный размер Ограниченных платежей в пользу связанных сторон ... превышал "
                "$400,000.00» — absolute USD cap. Party scope: «под связанной стороной понимается "
                "любое лицо, признанное связанной стороной Заёмщика по данным досье «Знай своего "
                "клиента» (KYC), независимо от назначения платежа, указанного в бухгалтерском "
                "учёте Заёмщика»."
            ),
        },
    },
    # ------------------------------------------------------------------ P4 / ACC-7804
    # contract 3c8ebcc791c3.pdf, Aktobe Grain Terminal JSC
    "P4": {
        "6.1": {
            "formula": "(revenue - opex) / revenue",
            "operator": ">=",
            "threshold": 0.28,
            "note": (
                "«отношение Скорректированной EBITDA к Выручке ... не менее 0.28x», and the clause "
                "defines the numerator itself: «Скорректированная EBITDA рассчитывается как "
                "Выручка за вычетом Операционных расходов с прибавлением разовых статей, "
                "признанных аудиторами Заёмщика подлежащими обратному добавлению согласно "
                "раскрытой учётной политике». ADD-BACK (not encoded in formula): auditor-approved "
                "one-off items are added back to (revenue - opex), subject to a MATERIALITY FLOOR "
                "— «статьи, не отвечающие установленному порогу существенности, к добавлению не "
                "принимаются». Denominator is plain Выручка, unadjusted."
            ),
        },
        "6.2": {
            "formula": "capex",
            "operator": "<=",
            "threshold": 1800000.0,
            "note": (
                "«совокупные расходы по указанной статье [«Капитальные затраты»] ... не превышали "
                "$1,800,000.00». Reclass rule is one-directional (in only): «Любая сумма, которую "
                "аудиторы Заёмщика признают подлежащей отражению как «Капитальные затраты», "
                "учитывается при расчёте независимо от счёта, на котором она была первоначально "
                "отражена» — unlike P2/B4 6.2, nothing is said about subtracting amounts "
                "reclassified OUT of capex."
            ),
        },
        "6.3": {
            "formula": "related_party / revenue",
            "operator": "<=",
            "threshold": 0.04,
            "note": (
                "«совокупные Ограниченные платежи в пользу аффилированных лиц ... превышали 0.04x "
                "от выручки за тот же период». Party scope by KYC dossier: «... а не назначением "
                "платежа»."
            ),
        },
    },
    # ------------------------------------------------------------------ P5 / ACC-7805
    # contract 2239b6b58f06.pdf, Ekibastuz Power Services JSC
    "P5": {
        "6.1": {
            "formula": "capex / (revenue - opex)",
            "operator": "<=",
            "threshold": 9.0,
            "note": (
                "«отношение совокупных капитальных затрат ГРУППЫ к EBITDA ЗАЁМЩИКА не превышало "
                "9.00x». SCOPE MISMATCH between numerator and denominator, and the category "
                "language cannot express it: «Капитальные затраты Группы определяются по "
                "КОНСОЛИДИРОВАННОЙ отчётности конечной материнской компании Группы и включают "
                "затраты ВСЕХ УЧАСТНИКОВ Группы» — the numerator is group-wide capex from the "
                "ultimate parent's consolidated statements, NOT the borrower's own capex. The "
                "denominator is borrower-only and is defined in the clause: «EBITDA Заёмщика "
                "рассчитывается по его собственной отчётности как Выручка за вычетом Операционных "
                "расходов». The formula above gives the correct arithmetic shape; the numerator "
                "`capex` must be read as GROUP capex, which is a different data scope from the "
                "borrower's ledger."
            ),
        },
        "6.2": {
            "formula": "revenue",
            "operator": ">=",
            "threshold": 7500000.0,
            "note": (
                "«поддерживать совокупный объём таких поступлений ... на уровне не менее "
                "$7,500,000.00», with «поступлениями по статье «Выручка» ... с учётом "
                "переквалификаций, произведённых аудиторами Заёмщика для целей соблюдения "
                "ковенантов» (identical wording to P1 6.2)."
            ),
        },
        "6.3": {
            "formula": "related_party",
            "operator": "<=",
            "threshold": 260000.0,
            "note": (
                "«совокупные платежи Заёмщика ... в адрес аффилированных и связанных сторон не "
                "должны превышать $260,000.00» — absolute USD cap. «Принадлежность контрагента к "
                "связанным сторонам устанавливается с учётом раскрытий в комплаенс-досье "
                "Заёмщика.»"
            ),
        },
    },
    # ------------------------------------------------------------------ P6 / ACC-7806
    # contract c10ebf055fa5.pdf, Taraz Cement Works JSC
    "P6": {
        "6.1": {
            "formula": "related_party / opex",
            "operator": "<=",
            "threshold": 0.08,
            "note": (
                "TRAP — denominator is OPEX, not revenue: «совокупный объём платежей в пользу "
                "связанных сторон ... превышал 0.08x ОПЕРАЦИОННЫХ РАСХОДОВ Заёмщика за этот "
                "период», reinforced by the heading «Максимальная доля платежей связанным "
                "сторонам в операционных расходах». Every sibling ratio-form 6.3 (P2/P4/P8/P10) "
                "divides by revenue; this one does not. No netting by category: «Платёж в пользу "
                "связанной стороны учитывается в полном объёме независимо от категории расходов, "
                "по которой он отражён» — the full payment counts in the numerator even though it "
                "is also sitting inside some expense category. Party scope: «круг связанных сторон "
                "определяется по досье идентификации клиента, ведущемуся подразделением комплаенс "
                "Кредитора»; «Операционные расходы определяются на основании аудированной "
                "отчётности Заёмщика»."
            ),
        },
        "6.2": {
            "formula": "revenue / (payroll + utilities)",
            "operator": ">=",
            "threshold": 3.0,
            "note": (
                "«Выручка ... составляла не менее 3.00x СОВОКУПНОЙ ВЕЛИЧИНЫ Расходов на оплату "
                "труда и Коммунальных расходов за этот период» — «совокупной величины» = the SUM "
                "of the two, i.e. payroll + utilities.  Deliberate contrast with B1 6.2, which "
                "uses the same two lines but says «по наибольшей» (max) and explicitly disclaims "
                "the sum. TRAP WORDING: «Расходы на оплату труда означают все выплаты персоналу и "
                "СВЯЗАННЫЕ С НИМИ расходы» — 'associated expenses' (payroll-related costs), NOT "
                "related-party expenses; this does not pull related_party into payroll. "
                "«Коммунальные расходы означают расходы на электроэнергию, водоснабжение и "
                "аналогичные поставки». Rearranged as a ratio for comparability; the literal test "
                "is revenue >= 3.00 * (payroll + utilities), which also holds when the denominator "
                "is zero."
            ),
        },
        "6.3": {
            "formula": "capex",
            "operator": "<=",
            "threshold": 1600000.0,
            "note": (
                "Note the numbering: in this contract 6.3 is the CAPEX cap and 6.1 is the "
                "related-party test — the reverse of most siblings. «совокупный объём расходов по "
                "статье «Капитальные затраты» ... превышал $1,600,000.00 ... любая сумма, "
                "переклассифицированная аудиторами Заёмщика в категорию «Капитальные затраты», "
                "включается в настоящий расчёт независимо от её первоначального отражения в учёте» "
                "— reclass IN only."
            ),
        },
    },
    # ------------------------------------------------------------------ P7 / ACC-7807
    # contract 6dd84ab9ef0e.pdf, Atyrau Pipeline Services JSC
    "P7": {
        "6.1": {
            "formula": "(tax + utilities) / ebitda",
            "operator": "<=",
            "threshold": 0.3,
            "note": (
                "«отношение суммы Налогов и Коммунальных расходов к EBITDA не превышало 0.30x». "
                "Numerator is the SUM of the two lines (contrast B1 6.2 / P10 6.2, which take a "
                "max). EBITDA is undefined in this contract (Статья 1 is boilerplate); siblings P5 "
                "and B1 define it as «Выручка за вычетом Операционных расходов». ACCRUAL RULE: "
                "«Начисленные, но не уплаченные в течение периода налоги учитываются наравне с "
                "уплаченными; их величина подтверждается учётными данными казначейства Заёмщика» — "
                "accrued-but-unpaid taxes count in the numerator alongside taxes actually paid, "
                "evidenced by treasury records rather than the cash ledger."
            ),
        },
        "6.2": {
            "formula": "revenue",
            "operator": ">=",
            "threshold": 8700000.0,
            "note": (
                "«совокупные поступления по статье «Выручка» ... не менее $8,700,000.00 ... сумма, "
                "переквалифицированная аудиторами Заёмщика в иную категорию, из расчёта "
                "исключается» — reclass OUT only; unlike P1/P5 6.2 nothing is added in."
            ),
        },
        "6.3": {
            "formula": "related_party",
            "operator": "<=",
            "threshold": 275000.0,
            "note": (
                "«не вправе прямо или косвенно перечислять, передавать или иным образом "
                "предоставлять связанным сторонам суммы, совокупный размер которых ... превышает "
                "$275,000.00» — absolute USD cap, direct AND indirect transfers. Carve-out (not "
                "encoded): «За исключением случаев, прямо согласованных Кредитором в письменной "
                "форме». Party scope: «любой контрагент, указанный в качестве такового в "
                "комплаенс-досье Заёмщика, независимо от того, описана ли операция в учёте как "
                "сделка со связанной стороной»."
            ),
        },
    },
    # ------------------------------------------------------------------ P8 / ACC-7808
    # contract 63e162bd710b.pdf, Kyzylorda Drilling Services JSC
    "P8": {
        "6.1": {
            "formula": "payroll",
            "operator": "<=",
            "threshold": 4000000.0,
            "note": (
                "TWO-COMPONENT metric that the category language can only half express. "
                "«Совокупные обязательства по персоналу означают СУММУ (а) всех расходов на оплату "
                "труда, понесённых Заёмщиком за период с 2025-01-01 по 2025-12-31, НЕЗАВИСИМО ОТ "
                "ТОГО, ОТРАЖЕНЫ ЛИ соответствующие суммы в его операционных записях, и (б) "
                "совокупного обязательства Заёмщика по любой программе выходных пособий, "
                "сокращения или удержания персонала, действующей на 2025-12-31, КАК РАСКРЫТО В "
                "ПРИМЕЧАНИЯХ к отчётности Заёмщика, независимо от наступления срока платежа по "
                "такой программе.» Component (а) = payroll, including payroll not booked in the "
                "operating records. Component (б) is an ADD-ON, not a transaction category: the "
                "balance of any severance / redundancy / retention programme in force at "
                "2025-12-31 taken from the disclosure notes, counted even though it is not yet "
                "due. The covenant metric is payroll + that disclosed liability; `formula` carries "
                "only the payroll leg. Also note the measurement date wording «по состоянию на "
                "2025-12-31» — a point-in-time cap, not a flow cap, even though leg (а) is a "
                "full-year flow."
            ),
        },
        "6.2": {
            "formula": "capex",
            "operator": "<=",
            "threshold": 2100000.0,
            "note": (
                "«совокупный объём расходов по статье «Капитальные затраты» ... превышал "
                "$2,100,000.00 ... любая сумма, переклассифицированная аудиторами Заёмщика в "
                "категорию «Капитальные затраты», включается в настоящий расчёт независимо от её "
                "первоначального отражения в учёте Заёмщика» — reclass IN only."
            ),
        },
        "6.3": {
            "formula": "related_party / revenue",
            "operator": "<=",
            "threshold": 0.04,
            "note": (
                "«совокупные Ограниченные платежи в пользу аффилированных лиц ... превышали 0.04x "
                "от выручки за тот же период». Party scope by KYC dossier, «а не назначением "
                "платежа»."
            ),
        },
    },
    # ------------------------------------------------------------------ P9 / ACC-7809
    # contract c459a9940b23.pdf, Zhezkazgan Mining Services JSC
    "P9": {
        "6.1": {
            "formula": None,
            "operator": "<=",
            "threshold": 0.15,
            "note": (
                "NOT EXPRESSIBLE in the category language: the numerator is a counterparty-filtered "
                "SUBSET of capex, not a category. «совокупная стоимость КАПИТАЛЬНЫХ АКТИВОВ, "
                "ПЕРЕДАННЫХ НЕОГРАНИЧЕННЫМ ДОЧЕРНИМ ОРГАНИЗАЦИЯМ за период ... превышала 0.15x "
                "совокупных капитальных затрат Заёмщика за этот период.» Intended computation: "
                "(value of capital assets transferred to Unrestricted Subsidiaries) / capex <= "
                "0.15. Definition of the filter: «Неограниченной дочерней организацией признаётся "
                "дочерняя организация, получившая соответствующий статус и, следовательно, "
                "находящаяся ВНЕ ОБЕСПЕЧЕНИЯ Кредитора, согласно досье идентификации клиента; "
                "передача дочерней организации, сохраняющей статус ОГРАНИЧЕННОЙ, в расчёт НЕ "
                "ВКЛЮЧАЕТСЯ.» So the numerator is NOT all related-party payments and NOT all "
                "capex: it is capex-type transfers whose counterparty is flagged Unrestricted in "
                "the KYC dossier. Transfers to Restricted subsidiaries are excluded even though "
                "they are related parties. Denominator is the borrower's total capex for the same "
                "period."
            ),
        },
        "6.2": {
            "formula": "revenue",
            "operator": ">=",
            "threshold": 6900000.0,
            "note": (
                "«совокупная выручка по статье «Выручка» ... не менее $6,900,000.00. Суммы, "
                "переклассифицированные аудиторами Заёмщика ИЗ указанной категории, ИСКЛЮЧАЮТСЯ "
                "из расчёта» — reclass OUT only."
            ),
        },
        "6.3": {
            "formula": "related_party",
            "operator": "<=",
            "threshold": 225000.0,
            "note": (
                "«не вправе совершать Ограниченные платежи в пользу связанных сторон на "
                "совокупную сумму свыше $225,000.00» — absolute USD cap. «Платежи в адрес "
                "контрагентов, отнесённых к связанным сторонам в досье идентификации клиента, "
                "включаются в указанную совокупную сумму независимо от их описания в учёте»."
            ),
        },
    },
    # ------------------------------------------------------------------ P10 / ACC-7810
    # contract bc5451f75caf.pdf, Karaganda Logistics Terminal JSC
    "P10": {
        "6.1": {
            "formula": "insurance / (lease + utilities)",
            "operator": ">=",
            "threshold": 0.2,
            "note": (
                "«отношение Страховых премий к сумме Арендных и Коммунальных расходов составляло "
                "не менее 0.20x». Numerator = insurance premiums; denominator = lease + utilities "
                "(sum, per «к сумме»). Reclass rule is auditor-acceptance gated: «Учитываются "
                "реклассификации, ПРИНЯТЫЕ аудиторами Заёмщика; реклассификации, рассмотренные и "
                "ОТКЛОНЁННЫЕ аудиторами, в расчёт не принимаются» — proposed-but-rejected "
                "reclassifications must be ignored."
            ),
        },
        "6.2": {
            "formula": "revenue - max(payroll, tax)",
            "operator": ">=",
            "threshold": 5000000.0,
            "note": (
                "«Выручка за вычетом НАИБОЛЬШЕЙ ИЗ ВЕЛИЧИН Расходов на оплату труда и Налогов "
                "составляла не менее $5,000,000.00. МЕНЬШАЯ ИЗ ДВУХ ВЕЛИЧИН В РАСЧЁТ НЕ "
                "ПРИНИМАЕТСЯ.» Only one of payroll/tax is deducted — whichever is larger — and the "
                "second sentence explicitly forbids deducting both. Not revenue - payroll - tax."
            ),
        },
        "6.3": {
            "formula": "related_party / revenue",
            "operator": "<=",
            "threshold": 0.05,
            "note": (
                "«совокупные Ограниченные платежи в пользу аффилированных лиц составили более "
                "0.05x от выручки за тот же период» — breach when strictly greater, so compliant "
                "while <= 0.05. «Контрагент считается аффилированным лицом, если он учтён как "
                "связанная сторона в досье «Знай своего клиента» (KYC) Заёмщика, независимо от "
                "описания соответствующей операции в учёте Заёмщика.»"
            ),
        },
    },
    # ------------------------------------------------------------------ B1 / ACC-7201
    # contract b5aecff2bbf2.pdf, Ekibastuz Energy JSC
    "B1": {
        "6.1": {
            "formula": "(revenue - opex) / interest",
            "operator": ">=",
            "threshold": 2.0,
            "note": (
                "«Коэффициент покрытия процентов означает отношение показателя EBITDA (ВЫРУЧКА ЗА "
                "ВЫЧЕТОМ ОПЕРАЦИОННЫХ РАСХОДОВ) к ПРОЦЕНТНЫМ РАСХОДАМ за период» — EBITDA is "
                "defined inline, so the formula is written out rather than using the `ebitda` "
                "category. «обязуется не допускать снижения ... ниже величины 2.00x» -> >= 2.00. "
                "Reclass rule: «с учётом любой переквалификации затрат В СОСТАВ Процентных "
                "расходов, принятой аудиторами Заёмщика» — costs reclassified INTO interest "
                "increase the denominator."
            ),
        },
        "6.2": {
            "formula": "max(payroll, utilities)",
            "operator": "<=",
            "threshold": 1500000.0,
            "note": (
                "TRAP — max, explicitly not a sum. «обязуется не допускать, чтобы КАКАЯ-ЛИБО "
                "ОТДЕЛЬНАЯ статья накладных расходов превышала $1,500,000.00 ... отдельными "
                "статьями накладных расходов признаются, ПО ОТДЕЛЬНОСТИ, А НЕ В СОВОКУПНОСТИ: (а) "
                "расходы на оплату труда и (б) расходы на коммунальные услуги ... Соблюдение "
                "проверяется ПО НАИБОЛЬШЕЙ из указанных сумм; ИХ СУММА НЕ ЯВЛЯЕТСЯ ПОКАЗАТЕЛЕМ "
                "настоящего ковенанта.» Deliberate contrast with P6 6.2, which sums the same two "
                "lines («совокупной величины»)."
            ),
        },
        "6.3": {
            "formula": "related_party",
            "operator": "<=",
            "threshold": 500000.0,
            "note": (
                "«совокупные платежи Заёмщика ... в адрес аффилированных и связанных сторон не "
                "должны превышать $500,000.00» — absolute USD cap. «Принадлежность контрагента к "
                "связанным сторонам устанавливается с учётом раскрытий в комплаенс-досье "
                "Заёмщика.» (Word-for-word the same clause as P5 6.3, different threshold.)"
            ),
        },
    },
    # ------------------------------------------------------------------ B4 / ACC-7204
    # contract be1e2186c99d.pdf, Shymkent Refinery JSC
    "B4": {
        "6.1": {
            "formula": "revenue",
            "operator": ">=",
            "threshold": 3500000.0,
            "note": (
                "PERIOD-RESTRICTED — the only covenant in the set that is not measured over the "
                "full covenant period. «обязуется обеспечить Выручку ЗА ЧЕТВЁРТЫЙ ФИНАНСОВЫЙ "
                "КВАРТАЛ периода, оканчивающегося 2025-12-31, в размере не менее $3,500,000.00 ... "
                "Выручка за четвёртый квартал включает ТОЛЬКО Выручку, ПРИЗНАННУЮ В ЭТОМ КВАРТАЛЕ» "
                "— i.e. revenue recognised 2025-10-01..2025-12-31 only, NOT full-year revenue. The "
                "formula language has no period dimension, so `revenue` here must be read as "
                "Q4-only revenue. Adjustment rule: «с учётом любой корректировки по методу "
                "начисления или ПЕРЕКВАЛИФИКАЦИИ ПЕРИОДА, принятой аудиторами Заёмщика» — auditor "
                "accrual/period reclassifications can move revenue into or out of Q4."
            ),
        },
        "6.2": {
            "formula": "capex",
            "operator": "<=",
            "threshold": 2000000.0,
            "note": (
                "«такие совокупные расходы за период с 2025-01-01 по 2025-12-31 превысили "
                "$2,000,000.00», where capex is «суммы, отнесённые к данной статье ... ВКЛЮЧАЯ "
                "суммы, переквалифицированные в неё независимым аудитором Заёмщика, и ЗА ВЫЧЕТОМ "
                "сумм, переквалифицированных аудитором ИЗ данной статьи» — bidirectional reclass "
                "(identical wording to P2 6.2). Full-year, unlike 6.1 of the same contract."
            ),
        },
        "6.3": {
            "formula": "related_party",
            "operator": "<=",
            "threshold": 500000.0,
            "note": (
                "«совокупный размер Ограниченных платежей в пользу связанных сторон ... превышал "
                "$500,000.00» — absolute USD cap. «под связанной стороной понимается любое лицо, "
                "признанное связанной стороной Заёмщика по данным досье «Знай своего клиента» "
                "(KYC), независимо от назначения платежа, указанного в бухгалтерском учёте "
                "Заёмщика»."
            ),
        },
    },
}

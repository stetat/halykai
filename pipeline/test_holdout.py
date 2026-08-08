"""Out-of-sample evaluation: what accuracy survives when the data is NOT what we tuned on.

Every other harness in this repo is in-sample. `validate` bracket-checks against the answer
key the rules were written while staring at; `reconstruct` synthesises a ledger that satisfies
whatever formula the engine already uses; `test_classifier`'s fixtures and `make_ledger`'s
narrations were both written alongside the keyword table they exercise. Those numbers measure
self-consistency. They cannot measure generalisation, and quoting them as "accuracy" overstates
what we know by a wide margin.

This module measures the two things that can honestly be measured from the practice release:

  PART 1  Classifier accuracy on HELD-OUT narrations — realistic KZ/RU bank payment strings
          written from domain knowledge, deliberately not reusing any wording from
          make_ledger.DESCRIPTIONS or test_classifier's fixtures, and seeded with the
          ambiguities a real ledger has (KZ "вознаграждение" = interest, pension
          contributions that look like tax, intra-group loans that look related-party).
          This is the one component whose event-day input is genuinely unseen.

  PART 2  Rule generality across borrowers. The engine never branches on a borrower id, so a
          metric rule is only trustworthy if the clause wording that triggers it recurs across
          borrowers. A rule firing for exactly one borrower is indistinguishable from a patch
          fitted to that borrower's cell, and carries no evidence it will fire correctly on an
          event-day contract.

    python -m pipeline.test_holdout
"""
from __future__ import annotations
import json
from collections import Counter, defaultdict

from . import config, engine
from .classifier import keyword_category, categorize_verbose, SIGN_FALLBACK
from .ledger import Txn
from .heldout_d import HELDOUT_D

R, O, C, L, P = engine.REVENUE, engine.OPEX, engine.CAPEX, engine.LEASE, engine.PAYROLL
U, T, I, N, F = (engine.UTILITIES, engine.TAX, engine.INTEREST,
                 engine.INSURANCE, engine.FINANCING)

# (description, counterparty, expected_category, is_inflow)
# Written to read like real Halyk/Kaspi statement narrations, not like the keyword table.
HELDOUT: list[tuple[str, str, str, bool]] = [
    # -- revenue -------------------------------------------------------------------
    ("Оплата по счёту №4471 за транспортно-экспедиторское обслуживание", "KTZ Express", R, True),
    ("Зачисление средств за отгруженную продукцию по спецификации 12", "AgroTrade LLP", R, True),
    ("Доход от оказания услуг хранения на терминале", "Caspian Storage", R, True),
    ("Payment received for stevedoring services, invoice INV-2211", "Black Sea Line", R, True),
    ("Поступление за реализованный щебень фракции 20-40", "StroyResurs LLP", R, True),
    # -- opex ----------------------------------------------------------------------
    ("Услуги охраны производственного объекта за июль", "Kazakhstan Security JSC", O, False),
    ("Ремонт и техническое обслуживание офисной техники", "TechService LLP", O, False),
    ("Приобретение спецодежды для персонала склада", "WorkWear KZ", O, False),
    ("Вывоз и утилизация производственных отходов", "EcoService LLP", O, False),
    ("Monthly subscription for warehouse management software", "SoftLine KZ", O, False),
    # -- capex ---------------------------------------------------------------------
    ("Оплата за поставку и монтаж линии сортировки", "PromMash JSC", C, False),
    ("Авансовый платёж по договору на реконструкцию цеха №3", "BuildInvest LLP", C, False),
    ("Приобретение основных средств: гусеничный экскаватор", "TechnoDealer", C, False),
    ("Модернизация подкрановых путей, этап 1", "MontazhStroy LLP", C, False),
    # -- lease ---------------------------------------------------------------------
    ("Арендная плата за складские помещения за июль 2025", "Almaty Warehouse LLP", L, False),
    ("Платёж по договору финансового лизинга №77-Л", "Halyk Leasing", L, False),
    ("Аренда земельного участка под перегрузочный комплекс", "Akimat Aktobe", L, False),
    # -- payroll -------------------------------------------------------------------
    ("Перечисление заработной платы работникам за июль", "Payroll batch", P, False),
    ("Выплата премии по итогам квартала персоналу", "Payroll batch", P, False),
    ("Обязательные пенсионные взносы за сотрудников", "ГЦВП", P, False),   # looks like tax
    ("Компенсация за неиспользованный трудовой отпуск", "Payroll batch", P, False),
    # -- utilities -----------------------------------------------------------------
    ("Оплата за потреблённую электроэнергию по счётчику", "AlmatyEnergoSbyt", U, False),
    ("Услуги теплоснабжения за отопительный сезон", "Teplo Company", U, False),
    ("Водоснабжение и водоотведение, июль 2025", "Vodokanal", U, False),
    # -- tax -----------------------------------------------------------------------
    ("Уплата НДС за 2 квартал 2025 года", "Комитет госдоходов", T, False),
    ("Социальный налог за июнь 2025", "Комитет госдоходов", T, False),
    ("Налог на имущество юридических лиц", "Комитет госдоходов", T, False),
    # -- interest ------------------------------------------------------------------
    ("Погашение начисленного вознаграждения по займу", "Halyk Bank", I, False),  # KZ for interest
    ("Оплата процентов по договору банковского займа №9912", "Halyk Bank", I, False),
    ("Interest payment on senior facility, period 07/2025", "Halyk Bank", I, False),
    # -- insurance -----------------------------------------------------------------
    ("Страхование грузов по генеральному полису", "Eurasia Insurance", N, False),
    ("Оплата страховой премии по КАСКО автопарка", "Nomad Insurance", N, False),
    # -- financing -----------------------------------------------------------------
    ("Получение кредитных средств по соглашению о кредитной линии", "Halyk Bank", F, True),
    ("Зачисление транша по договору банковского займа", "Development Bank of KZ", F, True),
    ("Drawdown under revolving credit facility", "Halyk Bank", F, True),
]


# ---------------------------------------------------------------------------------------
# SET B — written AFTER the keyword table was repaired against set A, which burned set A as
# training data. Set B is the honest estimate. Two deliberate differences from set A:
#   * counterparties are neutral ТОО/АО names that do NOT encode the category. Set A leaked
#     labels through strings like "Payroll batch", inflating its score.
#   * it includes constructions nobody patched for — "возведение", "устройство тупика",
#     "материальная помощь", principal repayments — because a test that only contains
#     what was just fixed measures nothing.
HELDOUT_B: list[tuple[str, str, str, bool]] = [
    # -- revenue -------------------------------------------------------------------
    ("Оплата за услуги по договору №112 от 01.03.2025", "ТОО Алатау Сервис", R, True),
    ("Экспортная выручка по контракту CIF Poti", "Trans Caspian Trading", R, True),
    ("Поступление от покупателя за партию №8", "ТОО Береке Астык", R, True),
    ("Agency fee income from freight forwarding", "Silk Road Logistics", R, True),
    # -- opex ----------------------------------------------------------------------
    ("Услуги по дезинсекции производственных помещений", "ТОО Санэпид Групп", O, False),
    ("Оплата типографских услуг и бланочной продукции", "ТОО Полиграф Астана", O, False),
    ("Курьерская доставка документов по РК", "АО Казпочта", O, False),
    ("Аудиторские услуги за 2024 год", "ТОО Аудит Партнёр", O, False),
    ("Оплата за услуги связи и интернет", "АО Казахтелеком", O, False),
    # -- capex ---------------------------------------------------------------------
    ("Оплата за проектно-изыскательские работы по строительству склада", "ТОО Проект КЗ", C, False),
    ("Приобретение серверного оборудования для ЦОД", "ТОО Ай Ти Дистрибьюшн", C, False),
    ("Затраты на устройство железнодорожного тупика", "ТОО Темир Жол Курылыс", C, False),
    ("Оплата по договору подряда на возведение навеса", "ТОО Алатау Курылыс", C, False),
    # -- lease ---------------------------------------------------------------------
    ("Субаренда офисных помещений в БЦ Нурлы Тау", "ТОО Капитал Плаза", L, False),
    ("Операционная аренда автотранспорта", "ТОО Рент Авто КЗ", L, False),
    # -- payroll -------------------------------------------------------------------
    ("Выплата материальной помощи работникам", "ТОО Астана Логистик", P, False),
    ("Начисление и выплата отпускных", "ТОО Астана Логистик", P, False),
    ("Удержания и перечисление ОПВ за июль", "ГЦВП", P, False),
    # -- utilities -----------------------------------------------------------------
    ("Электроснабжение производственной площадки", "ТОО Энергия Астана", U, False),
    ("Оплата за отпущенную тепловую энергию", "ТОО Астана Энергия", U, False),
    # -- tax -----------------------------------------------------------------------
    ("Уплата КПН за 2024 год", "УГД по Алматинскому району", T, False),
    ("Пеня и штраф по налоговой задолженности", "УГД по Алматинскому району", T, False),
    ("Индивидуальный подоходный налог за работников", "УГД по Алматинскому району", T, False),
    # -- interest ------------------------------------------------------------------
    ("Начисленное вознаграждение по облигационному займу", "АО Банк ЦентрКредит", I, False),
    ("Оплата процентов за пользование овердрафтом", "АО Банк ЦентрКредит", I, False),
    # -- insurance -----------------------------------------------------------------
    ("Страхование ответственности перевозчика", "АО Нурполис", N, False),
    ("Продление полиса страхования имущества", "АО Нурполис", N, False),
    # -- financing -----------------------------------------------------------------
    ("Погашение части основного долга по кредитной линии", "АО Банк ЦентрКредит", F, False),
    ("Repayment of principal under term loan", "Eurasian Development Bank", F, False),
    ("Поступление денег по договору о предоставлении займа", "АО Банк ЦентрКредит", F, True),
]


# ---------------------------------------------------------------------------------------
# SET C — written after set B was spent on the payroll/возведение repairs. Deliberately
# probes a RISK those repairs introduced: "вознаграждение" was mapped to INTEREST because it
# is the KZ banking term for it, but the same word means an ordinary fee or director's
# remuneration. Set C contains all three senses so the trade-off is measured, not assumed.
HELDOUT_C: list[tuple[str, str, str, bool]] = [
    # -- revenue -------------------------------------------------------------------
    ("Оплата за перевозку груза по маршруту Актау-Алматы", "ТОО Каспий Транс", R, True),
    ("Зачисление по инкассо от контрагента", "ТОО Береке Астык", R, True),
    ("Proceeds from sale of scrap metal", "Metal Trade GmbH", R, True),
    ("Комиссионное вознаграждение агента", "ТОО Агент Групп", R, True),      # fee, NOT interest
    # -- opex ----------------------------------------------------------------------
    ("Оплата за питьевую воду в офис", "ТОО Тассай", O, False),
    ("Услуги по разработке сайта компании", "ТОО Веб Студия", O, False),
    ("Членские взносы в отраслевую ассоциацию", "Ассоциация перевозчиков РК", O, False),
    ("Представительские расходы за июль", "ТОО Астана Логистик", O, False),
    # -- capex ---------------------------------------------------------------------
    ("Закуп погрузочной техники", "ТОО Техно Дилер", C, False),
    ("Дооборудование склада стеллажными системами", "ТОО Складские Решения", C, False),
    ("Строительно-монтажные работы на объекте", "ТОО Алатау Курылыс", C, False),
    # -- lease ---------------------------------------------------------------------
    ("Аренда специальной техники с экипажем", "ТОО Рент Техника", L, False),
    # -- payroll -------------------------------------------------------------------
    ("Перечисление заработной платы за вторую половину месяца", "ТОО Астана Логистик", P, False),
    ("Оплата больничных листов", "ТОО Астана Логистик", P, False),
    ("Вознаграждение членам совета директоров", "ТОО Астана Логистик", P, False),  # not interest
    # -- utilities -----------------------------------------------------------------
    ("Оплата за газоснабжение котельной", "АО КазТрансГаз", U, False),
    ("Холодное водоснабжение за июль", "ГКП Водоканал", U, False),
    # -- tax -----------------------------------------------------------------------
    ("Земельный налог за 3 квартал", "УГД по Алматинскому району", T, False),
    ("Таможенная пошлина при импорте оборудования", "Комитет таможенного контроля", T, False),
    # -- interest ------------------------------------------------------------------
    ("Уплата вознаграждения по договору банковского займа", "АО Банк ЦентрКредит", I, False),
    ("Начисленные проценты по овердрафту", "АО Банк ЦентрКредит", I, False),
    # -- insurance -----------------------------------------------------------------
    ("Обязательное страхование работников от несчастных случаев", "АО Нурполис", N, False),
    # -- financing -----------------------------------------------------------------
    ("Получение краткосрочного займа от банка", "АО Банк ЦентрКредит", F, True),
    ("Погашение задолженности по кредитной линии", "АО Банк ЦентрКредит", F, False),
]


def _mk(desc: str, cp: str, inflow: bool) -> Txn:
    amt = 250_000.0 if inflow else -250_000.0
    return Txn("TXN-H-0001", "ACC-7801", "2025-07-15", amt, "USD", cp, desc, "P1")


def part1_classifier(data=HELDOUT, label="SET A") -> tuple[int, int]:
    print(f"{label} — classifier on held-out narrations")
    print("-" * 78)
    wrong: list[tuple[str, str, str]] = []
    per_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for desc, cp, want, inflow in data:
        got = keyword_category(_mk(desc, cp, inflow))
        per_cat[want][1] += 1
        if got == want:
            per_cat[want][0] += 1
        else:
            wrong.append((desc, want, got))
    ok = sum(v[0] for v in per_cat.values())
    tot = sum(v[1] for v in per_cat.values())

    for cat in sorted(per_cat):
        hit, n = per_cat[cat]
        flag = "" if hit == n else "   <-- misses"
        print(f"  {cat:<12} {hit:>2}/{n:<2}{flag}")
    if wrong:
        print("\n  misclassified:")
        for desc, want, got in wrong:
            print(f"    want {want:<10} got {got:<12} | {desc[:56]}")
    print(f"\n  {label} ACCURACY: {ok}/{tot} = {ok / tot:.1%}")
    return ok, tot


def part2_rule_generality() -> tuple[int, int]:
    print("\n\nPART 2 — do the metric rules generalise across borrowers?")
    print("-" * 78)
    specs = json.loads(config.ROOT.joinpath("covenant_specs.json").read_text(encoding="utf-8"))
    kind_borrowers: dict[str, set[str]] = defaultdict(set)
    cells: list[tuple[str, str, str]] = []
    unparsed = 0
    for sc, entry in specs.items():
        for cid, spec in entry.get("covenants", {}).items():
            kind = engine.classify_kind(spec)
            kind_borrowers[kind].add(sc)
            cells.append((sc, cid, kind))
            if spec.get("threshold") is None or not spec.get("operator"):
                unparsed += 1

    print(f"  {'metric kind':<22} {'cells':>5}  {'borrowers':>9}   verdict")
    for kind, borrowers in sorted(kind_borrowers.items(),
                                  key=lambda kv: -len(kv[1])):
        n_cells = sum(1 for _, _, k in cells if k == kind)
        verdict = ("generalises" if len(borrowers) >= 3 else
                   "weak (2 borrowers)" if len(borrowers) == 2 else
                   "SINGLE BORROWER — unverifiable")
        print(f"  {kind:<22} {n_cells:>5}  {len(borrowers):>9}   {verdict}")

    solo = {k for k, b in kind_borrowers.items() if len(b) == 1}
    solo_cells = [(sc, cid, k) for sc, cid, k in cells if k in solo]
    corroborated = len(cells) - len(solo_cells)
    print(f"\n  cells whose rule is corroborated by >=2 borrowers: "
          f"{corroborated}/{len(cells)} = {corroborated / len(cells):.1%}")
    if solo_cells:
        print("  cells resting on a single-borrower rule (no generalisation evidence):")
        for sc, cid, k in solo_cells:
            print(f"    {sc:>4} {cid}   {k}")
    if unparsed:
        print(f"  !! {unparsed} cells have no parsed threshold/operator")
    return corroborated, len(cells)


def part3_provenance() -> None:
    """How many answers were EARNED by a rule, and how many fell out of the amount's sign?

    The sign fallback returns revenue for any unmatched credit and opex for any unmatched
    debit. On a ledger that is mostly debits it is right often enough to prop up an accuracy
    score while knowing nothing. Splitting the two apart is the difference between "the
    classifier handles this" and "the arithmetic of the test set happened to agree"."""
    print("\n\nPART 3 — was the answer earned, or guessed from the sign?")
    print("-" * 78)
    pooled = HELDOUT + HELDOUT_B + HELDOUT_C
    buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0])   # [correct, total]
    contested_wrong: list[tuple[str, str, str]] = []
    for desc, cp, want, inflow in pooled:
        t = _mk(desc, cp, inflow)
        got, rule, contested = categorize_verbose(t)
        key = ("sign-fallback" if rule == SIGN_FALLBACK
               else "contested" if contested else "clean rule")
        buckets[key][1] += 1
        if got == want:
            buckets[key][0] += 1
        elif contested:
            contested_wrong.append((desc, want, got))

    for key in ("clean rule", "contested", "sign-fallback"):
        hit, n = buckets[key]
        if not n:
            continue
        print(f"  {key:<15} {hit:>3}/{n:<3} = {hit / n:>6.1%}   ({n / len(pooled):.0%} of items)")
    sf = buckets["sign-fallback"]
    print(f"\n  {sf[1]} of {len(pooled)} pooled items ({sf[1] / len(pooled):.0%}) carry NO vocabulary")
    print("  match at all — their category is inferred from the sign of the amount alone.")
    if contested_wrong:
        print("\n  contested items decided wrongly by _RULES ordering:")
        for desc, want, got in contested_wrong:
            print(f"    want {want:<10} got {got:<11} | {desc[:52]}")


def main() -> None:
    print("=" * 78)
    print("OUT-OF-SAMPLE EVALUATION")
    print("=" * 78 + "\n")
    a_ok, a_tot = part1_classifier(HELDOUT, "PART 1A (BURNED — the keyword table was "
                                            "repaired against this set; now training data)")
    print()
    b_ok, b_tot = part1_classifier(HELDOUT_B, "PART 1B (BURNED — payroll/возведение repairs "
                                              "were fitted to this set)")
    print()
    c_ok, c_tot = part1_classifier(HELDOUT_C, "PART 1C (BURNED — the вознаграждение-precision "
                                              "and stem repairs were fitted to this set)")
    print()
    d_ok, d_tot = part1_classifier(HELDOUT_D, "PART 1D (BLIND — written by an agent that never "
                                              "read the keyword table)")
    corr, ncells = part2_rule_generality()
    part3_provenance()

    print("\n\n" + "=" * 78)
    print("HONEST SUMMARY")
    print("=" * 78)
    # ALL THREE sets are now training data. The only out-of-sample numbers this repo will ever
    # have from them are the FIRST-CONTACT scores, recorded here so they cannot be quietly
    # replaced by the flattering post-fix ones. Their mean is the accuracy estimate to plan on.
    first = [("A", 27, 35), ("B", 25, 30), ("C", 19, 24), ("D", 42, 60)]
    print("  Every set was written BEFORE the round of fixes it motivated, and burned by them.")
    print("  A set's value is its FIRST-CONTACT score; the post-fix score is self-congratulation.\n")
    print(f"  {'set':<5} {'first contact':>16}   {'now (burned)':>14}")
    for name, fo, ft in first:
        now = {"A": (a_ok, a_tot), "B": (b_ok, b_tot),
               "C": (c_ok, c_tot), "D": (d_ok, d_tot)}[name]
        print(f"  {name:<5} {f'{fo}/{ft} = {fo / ft:.1%}':>16}   "
              f"{f'{now[0]}/{now[1]} = {now[0] / now[1]:.1%}':>14}")
    fo, ft = sum(x[1] for x in first), sum(x[2] for x in first)
    print(f"\n  POOLED FIRST-CONTACT ACCURACY : {fo}/{ft} = {fo / ft:.1%}   <- quote this")
    print("  No unburned set remains. A further number requires narrations written by someone")
    print("  who has not read the keyword table, or the real event-day ledger.")
    print(f"\n  metric rules corroborated       : {corr}/{ncells} = {corr / ncells:.1%}")
    print("  `actual` values                 : UNMEASURABLE (real ledger withheld)")
    print("\n  The 36/36 from reconstruct.py and the 34/36 from validate.py are in-sample")
    print("  and must not be reported as accuracy.")


if __name__ == "__main__":
    main()

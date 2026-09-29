Да. Для вашей задачи я бы изначально строил **не приложение вокруг конкретной модели, а геолокационный inference framework**, где `imajev`, Chipoint и их комбинации являются взаимозаменяемыми стратегиями. Тогда через несколько тысяч тестовых Street View кадров вы сможете не гадать, какая архитектура лучше, а выбрать её по своим данным.

Причём ваша первоначальная идея с `континент → страна → регион` хорошая, но её стоит немного изменить: **не делать жёсткий каскад, где один неправильный ответ закрывает остальные ветви**. HierLoc как раз показал это экспериментально: на OSV-5M жадный hierarchical beam=1 дал 79,4% country / 48,9% region, а сохранение 10 ветвей дало 82,9% / 55,0%. :chatgpt-content-reference{index="0"}

## 1. Архитектура, которую я бы заложил

Внутри системы положение должно представляться не одним ответом `"Germany"`, а **распределением вероятности по Земле**.

```text
                         SCREENSHOT
                             │
                             ▼
                    IMAGE NORMALIZATION
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              ▼              ▼              ▼
        CHIPOINT EXPERT   IMAJEV EXPERT   OPTIONAL
                                        HIERLOC/GeoSURGE
              │              │              │
              ▼              ▼              ▼
        GeoEvidence      GeoEvidence      GeoEvidence
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                      FRAME FUSION
                             │
                             ▼
                SPATIAL POSTERIOR P(x,y)
                             │
                             ▼
                    TEMPORAL FUSION
                     frame 1 → frame N
                             │
                ┌────────────┼────────────┐
                ▼            ▼            ▼
              COUNTRY      ADMIN-1      GEO CELLS
                │            │            │
             Germany       Hessen      probability
               0.71          0.38          map
```

**Это главный архитектурный принцип.**

Ни Chipoint, ни imajev не должны напрямую управлять состоянием карты. Они только предоставляют evidence.

Это позволит через конфигурацию запускать:

```text
method = imajev_flat
method = imajev_hierarchical
method = chipoint_raw
method = chipoint_full
method = late_fusion
method = chipoint_jev_rerank
method = learned_fusion
```

и получать одинаковый выходной формат.

---

# 2. Чем ваша идея принципиально отличается от Chipoint v2

Ваш подход — **classification**.

Вы спрашиваете модель:

```text
Что это?

Europe       0.94
Asia         0.03
...

Что внутри Europe?

Germany      0.43
Austria      0.31
Czechia      0.08
...

Что внутри Germany?

Hessen       0.32
Bayern       0.26
...
```

Это попытка понять изображение как совокупность признаков и соотнести их с географическим классом.

Chipoint v2 работает совершенно иначе.

Его street-view arm использует большую галерею из **4,89 млн OSV-5M кадров**. Для изображения формируются embeddings несколькими vision towers; в опубликованной демонстрации используются DINOv3 ViT-L/16 и два OpenCLIP/SigLIP2-представления. Каждый embedding проецируется в нормализованный 512-D вектор, после чего выполняется поиск похожих кадров. Для каждого tower берётся top-200, scores нормализуются, списки объединяются, и затем остаются лучшие 64 кандидата. :chatgpt-content-reference{index="1"}

То есть логика примерно такая:

```text
                         QUERY
                           │
          ┌────────────────┼────────────────┐
          │                │                │
       DINOv3          SigLIP2-A        SigLIP2-B
          │                │                │
        512d             512d             512d
          │                │                │
          ▼                ▼                ▼
     OSV gallery       OSV gallery       OSV gallery
          │                │                │
       top-200           top-200           top-200
          └────────────────┼────────────────┘
                           ▼
                    score fusion
                           │
                        top-64
                           │
                     23-feature
                      reranker
                           │
                           ▼
                 geographic candidates
```

Полный Chipoint v2 с reranker существенно превосходит его собственный raw retrieval: авторы сообщают рост Acc@25 km с **34,89% до 44,8%**, Acc@200 km с 61,96% до 81,1% и медианную ошибку 32 км. Это self-reported результаты на OSV-5M. :chatgpt-content-reference{index="2"}

Поэтому системы действительно **комплементарны**:

| | imajev | Chipoint |
|---|---|---|
| Принцип | semantic classification | visual retrieval |
| «Знает» географию | через параметры VLM | через реальные геопривязанные изображения |
| Дорожный знак | понимает семантику | ищет визуально похожие |
| Необычный пейзаж | может рассуждать | ищет аналоги |
| Новая страна/регион | можно добавить как вариант | нужна gallery coverage |
| Координаты | непосредственно не предназначен | естественный результат |
| `unknown` | есть специально обученный | требуется строить confidence |
| Ошибки | semantic/VLM | retrieval/domain/gallery |

Именно поэтому я **не выбирал бы сейчас между ними**.

---

# 3. Общий внутренний формат результата

Это самый важный элемент будущей архитектуры.

Каждый метод должен возвращать условно такой объект:

```text
GeoEvidence

source:
    "imajev_hierarchical"
    "chipoint_street"
    "chipoint_imajev_rerank"
    ...

model_version

country_scores:
    DE  -1.42
    AT  -1.83
    CH  -2.91
    CZ  -3.17

admin1_scores:
    DE-HE  -2.15
    DE-BY  -2.42
    AT-5    -2.71
    ...

spatial_candidates:
    lat       lon       score
    50.08     8.24      ...
    49.91     8.62      ...
    ...

spatial_cells:
    cell_id → log_likelihood

unknown_probability

diagnostics:
    entropy
    top1_top2_gap
    candidate_spread_km
    ...
```

Обратите внимание: внутри лучше хранить **log-scores / log-likelihood**, а не пытаться всё немедленно называть «вероятностью».

Потому что:

- `0.82` из softmax imajev;
- cosine similarity `0.82` от DINO;
- score reranker `0.82`

— три совершенно разных числа.

Их нельзя просто усреднить.

---

# 4. Главным представлением я бы сделал не страны, а географические ячейки

Хотя интерфейс вам нужен:

```text
страна
регион
тепловая карта
```

внутренняя модель должна жить в пространстве:

```text
world → geocells
```

Например, H3/S2 или собственная иерархическая сетка.

Условно:

```text
cell 81a93...      0.018
cell 81a94...      0.031
cell 81a95...      0.087
cell 81a96...      0.124
...
```

А уже потом:

```text
P(Germany) =
Σ P(cell), cell ∈ Germany
```

и:

```text
P(Hessen) =
Σ P(cell), cell ∈ Hessen
```

Это даёт важное преимущество.

Например Chipoint получил:

```text
Frankfurt      0.20
Wiesbaden      0.17
Mainz          0.15
Darmstadt      0.11
Mannheim       0.07
```

На административном уровне это странно разделяется между Hessen и Rheinland-Pfalz.

Пространственная модель покажет нормальный непрерывный hotspot вокруг Rhine-Main.

---

# 5. Первый независимый эксперт: чистый imajev

У imajev-4b есть очень полезное свойство: каждый вариант ответа сопоставляется одному из **255 decision codes**, а результат получается чтением logits, без autoregressive generation. Также `unknown` является отдельным обученным исходом. :chatgpt-content-reference{index="3"}

Поэтому я бы реализовал **три разных режима imajev**, а не один.

### Imajev A — flat country

Самый интересный baseline:

```text
IMAGE

Where was this Google Street View image most likely captured?

Albania
Algeria
Andorra
...
Germany
...
Zimbabwe
```

Практически все страны мира помещаются в предел 255 вариантов.

Получаем сразу:

```text
Germany       .38
Austria       .21
Switzerland   .08
Czechia       .06
Belgium       .04
...
unknown       .02
```

Я **не утверждаю, что это окажется хорошо**. И именно поэтому этот вариант нужен как эксперимент.

Он чрезвычайно простой и станет отличным baseline.

---

# 6. Imajev B — ваша иерархическая схема

Здесь уже:

```text
continent
    ↓
country
    ↓
admin1
```

Но **без winner-takes-all**.

Допустим:

```text
Europe          .91
North America   .05
Asia            .03
```

Дальше рассматриваем не одну Европу, а top-N ветвей.

После country:

```text
Germany      .37
Austria      .24
Czechia      .13
Switzerland  .08
Slovenia     .04
```

а затем регионы для, например, top-4 стран.

Это практически тот же принцип, который HierLoc использует при hierarchical beam search. У HierLoc DINOv3 версия на OSV-5M получила 82,9% country, 55,0% region, 40,7% subregion и 23,3% city, причём beam=10 оказался заметно лучше жадного beam=1. :chatgpt-content-reference{index="4"}

Поэтому **континент нельзя использовать как жёсткий фильтр**.

Я бы трактовал его как дополнительный evidence.

---

# 7. Континент вообще не должен быть обязательным уровнем

Это небольшое, но важное отличие от вашей первоначальной схемы.

Если модель говорит:

```text
Europe 0.52
Asia   0.45
```

это может быть Турция.

Если выбрать Europe и удалить Asia, вы искусственно уничтожили правильную гипотезу.

Поэтому:

```text
continent
```

я бы оставил как **auxiliary classifier**, который усиливает или ослабляет вероятности, но никогда не закрывает географию.

Основной hierarchy:

```text
country
   ↓
admin1
   ↓
optional admin2/cell
```

---

# 8. Imajev C — semantic evidence

Это ещё один совершенно независимый эксперимент.

Вместо вопроса:

> Где фотография?

мы спрашиваем:

```text
Driving side?
left / right / unclear

Road-line convention?
...

Dominant script?
Latin / Cyrillic / Arabic / ...

Terrain?
flat / rolling / mountainous / ...

Climate / vegetation?
...

Roadside bollards family?
...

License plate appearance?
...

Urban morphology?
...
```

И imajev может вернуть сразу несколько typed decisions в одном запросе; его сервер поддерживает несколько вопросов к одному state/image, хотя каждый вопрос требует своего forward readout после общего input preparation. :chatgpt-content-reference{index="5"}

Но я бы **не стал вручную писать правила**:

```text
white bollard + red reflector = Germany
```

Лучше на тренировочной выборке автоматически обучить:

\[
P(country\mid cues)
\]

То есть imajev превращается в **feature extractor**, а маленький classifier изучает, какие сочетания признаков действительно географически информативны.

Это существенно устойчивее человеческого списка GeoGuessr-правил.

---

# 9. Второй независимый эксперт — Chipoint v2 street arm

Поскольку вы заранее знаете, что вход — **Google Street View**, general-photo router Chipoint вам вообще не нужен.

Всегда:

```text
arm = street
```

Это упрощает архитектуру.

При этом я бы специально сделал абляции самого Chipoint:

| Режим | Что проверяем |
|---|---|
| `chipoint_dino` | один DINOv3 tower |
| `chipoint_siglip` | один SigLIP2 tower |
| `chipoint_multi_raw` | несколько towers + fusion |
| `chipoint_full` | towers + retrieval + штатный reranker |
| `chipoint_full_custom_index` | то же, но оптимизированный ANN index |

Это важно ещё и для вашей 3090 Ti.

Возможно окажется, что:

```text
DINO only = 95% качества full pipeline
```

при заметно меньшем времени.

А может оказаться, что второй tower критичен для страны.

Угадывать здесь не надо — это идеальный кандидат на benchmark.

---

# 10. Но Chipoint выдаёт точки. Как получить из него вероятность страны?

Вот здесь начинается наша собственная архитектура.

Допустим top-K Chipoint:

```text
rank   country   region        lat      lon       score
1      DE        Hessen        ...      ...       s1
2      DE        Hessen        ...      ...       s2
3      AT        Salzburg      ...      ...       s3
4      DE        Bayern        ...      ...       s4
5      CZ        ...           ...      ...       s5
...
```

Сначала преобразуем raw score в вес:

\[
w_i =
\frac{\exp(s_i/T_C)}
{\sum_j \exp(s_j/T_C)}
\]

`T_C` — температура, которую мы **обучим на Google Street View validation set**.

Затем:

\[
P_C(country=c)
=
\sum_{i:c_i=c}w_i.
\]

Например:

```text
Germany     .56
Austria     .22
Czechia     .08
Switzerland .05
...
```

То же самое делаем для admin-1.

Но ещё лучше не терять координаты.

---

# 11. Из Chipoint я бы строил spatial likelihood

Каждая retrieved point создаёт вокруг себя вероятностное ядро:

\[
L_C(g)=
\sum_i w_i K(d(g,g_i);\sigma_i)
\]

где:

- \(g_i\) — координата retrieved candidate;
- \(d\) — great-circle distance;
- \(K\) — Gaussian/von-Mises/Fisher kernel;
- \(\sigma\) — пространственная неопределённость.

Получится буквально:

```text
                  high
                   █
                ███████
              ███████████
            ███████████████
              Frankfurt
          ███████████████████
             Rhine-Main
                █████
                  █
                  low
```

Это уже именно ваша будущая heatmap.

Не надо превращать Chipoint сразу в `"Germany"`.

---

# 12. Особо интересный режим: Chipoint → imajev reranker

Вот этот вариант я считаю **одним из наиболее вероятных победителей эксперимента**.

Сначала Chipoint делает то, что умеет лучше всего:

```text
4.89M geographic gallery
          ↓
       top 64
          ↓
country aggregation
          ↓

Germany       .41
Austria       .24
Switzerland   .12
Czechia       .09
Slovenia      .05
France        .04
```

А затем imajev получает **не 200 стран**, а только:

```text
Based only on the image, which of these candidate countries
best matches this location?

Germany
Austria
Switzerland
Czechia
Slovenia
France
None / insufficient evidence
```

Получает:

```text
Germany       .19
Austria       .58
Switzerland   .10
...
```

После чего мы объединяем оба evidence.

Это сильно отличается от вопроса VLM:

> «Где находится фотография?»

Мы используем Chipoint как **candidate generator**, а imajev как **semantic reranker**.

---

# 13. Можно сделать и наоборот: imajev как prior для Chipoint

Не:

```text
imajev сказал Austria
→ удаляем все остальные страны
```

а:

\[
S_i'=S_i+\lambda\log P_J(c_i)
\]

То есть candidate Chipoint из Австрии получает небольшой бонус, если imajev считает Австрию вероятной.

Если:

```text
Chipoint:

Germany   candidate score 8.2
Austria   candidate score 8.0

imajev:

Germany .20
Austria .62
```

совместный scorer вполне может переставить кандидатов.

Но если imajev категорически ошибся:

```text
Austria .03
```

Chipoint всё равно имеет возможность сохранить Австрию.

Это принципиально лучше hard filtering.

---

# 14. Основной late fusion

После калибровки каждого эксперта я бы использовал логарифмическое объединение.

Пусть:

\[
L_C(g)
\]

— spatial likelihood Chipoint,

а:

\[
L_J(g)
\]

— likelihood imajev.

Тогда:

\[
\log P(g|I)
=
\log P_0(g)
+
\alpha \log L_C(g)
+
\beta \log L_J(g)
-Z.
\]

Не надо выбирать `α=0.5`, `β=0.5` руками.

Их надо определить на validation set.

Например эксперимент покажет:

```text
αChipoint = 0.72
βImajev   = 0.28
```

или совершенно наоборот.

---

# 15. Почему я говорю именно о likelihood, а не просто probability

Представьте, что в training data много Германии.

Тогда модель может иметь априорную склонность:

```text
Germany .10
```

ещё до просмотра фотографии.

Если напрямую перемножить вероятности нескольких моделей, этот prior учитывается несколько раз.

Поэтому правильнее использовать приблизительное **likelihood ratio**:

\[
L_J(c)
=
\frac{P_J(c|I)}
{P_0(c)}.
\]

Например:

```text
model output Germany     = .20
prior Germany            = .05

evidence ratio           = 4×
```

А:

```text
model output Luxembourg  = .04
prior Luxembourg         = .001
```

может оказаться гораздо более сильным evidence.

Это особенно важно для стран с очень разной представленностью Street View.

---

# 16. Третий уровень — обученный fusion model

После того как накопится benchmark, вообще не обязательно вручную выбирать формулу.

На каждый кадр можно собирать, например:

```text
Chipoint:
top1 score
top2 score
top1-top2 margin
entropy
candidate geographic spread
country concentration
admin1 concentration
DINO top score
SigLIP top score
DINO/SigLIP agreement
number of towers supporting candidate

imajev:
country probability
unknown probability
country entropy
continent probability
road-side result
script result
terrain result
...

temporal:
previous country posterior
previous region posterior
embedding novelty
```

На этих признаках можно обучить очень маленький:

```text
LightGBM
```

или MLP.

Его задача:

> Насколько candidate \(c,r\) заслуживает доверия?

Это гораздо проще, чем обучать ещё одну огромную vision-модель.

И это даст вам **learned ensemble**.

---

# 17. Я бы поэтому заложил восемь экспериментальных стратегий

| ID | Метод | Смысл |
|---|---|---|
| J1 | `imajev_flat_country` | один Jev Choice по всем странам |
| J2 | `imajev_hierarchical` | country→region с beam |
| J3 | `imajev_semantic` | набор географических визуальных признаков |
| C1 | `chipoint_raw` | retrieval без reranker |
| C2 | `chipoint_full` | полный street-view pipeline |
| F1 | `late_fusion` | независимый Chipoint + imajev |
| F2 | `chipoint_then_jev` | Chipoint генерирует candidates, imajev rerank |
| F3 | `learned_fusion` | метамодель по outputs всех экспертов |

Я бы **не удалял эти реализации даже после выбора победителя**.

В настройках приложения:

```text
Inference profile

○ Imajev
○ Chipoint
○ Fusion
○ Experimental comparison
● Best validated model
```

а `Best validated model` просто указывает на конкретный зарегистрированный pipeline.

---

# 18. Есть ещё один очень интересный кандидат: HierLoc-подобная ветвь

Ваша идея удивительно близка к HierLoc, только там вместо словесной классификации создаются embeddings географических сущностей:

```text
Country
  └── Region
       └── Subregion
            └── City
```

и image embedding сопоставляется непосредственно с ними.

HierLoc заменяет поиск по миллионам изображений примерно **240 тыс. geographic entity embeddings**, использует hyperbolic representation и geographic-distance-aware contrastive learning. :chatgpt-content-reference{index="6"}

Для вашей задачи это очень хороший **третий независимый эксперт**:

```text
imajev       semantic reasoning
Chipoint     image retrieval
HierLoc      geographic entity embedding
```

У этих методов совершенно разные failure modes.

---

# 19. GeoSURGE показывает ещё один путь объединения

В 2026 году GeoSURGE пошёл ещё дальше: там изображение представляется одновременно через appearance features и semantic segmentation, а мир — как иерархия learned geographic embeddings. Фичи изображения и сегментации объединяются cross-attention. Авторы CVPR 2026 сообщают улучшения на 22 из 25 benchmark metrics и связывают значительную часть выигрыша именно с geographic hierarchy и semantic fusion. :chatgpt-content-reference{index="7"}

То есть ваша идея:

> «Не полагаться на одно общее визуальное сходство, а добавить семантическое понимание сцены»

не только разумна — это прямо соответствует современному направлению исследований.

Но imajev позволит сделать это намного проще экспериментально, без обучения полноценной GeoSURGE.

---

# 20. Теперь самое важное для вашего приложения — несколько кадров

Здесь у вас есть огромное преимущество над benchmark.

Chipoint, HierLoc и большинство статей оцениваются как:

```text
ONE IMAGE
     ↓
location
```

А у вас:

```text
frame 1
frame 2
frame 3
...
frame 15
```

из одной игровой локации.

И это потенциально **намного более мощный сигнал**.

Допустим:

```text
FRAME 1
Germany   .42
Austria   .31

FRAME 2
Germany   .61
Austria   .18

FRAME 3
Germany   .36
Austria   .40

FRAME 4
Germany   .71
Austria   .12
```

Нельзя просто усреднить.

Лучше вести posterior.

---

# 21. Накопление evidence

Для каждого кадра получаем:

\[
P_t(g).
\]

Храним накопленное:

\[
Q_t(g).
\]

При новом кадре:

\[
\log Q_t(g)
=
\log Q_{t-1}(g)
+
w_t
\left[
\log P_t(g)-\log P_0(g)
\right].
\]

После этого normalise.

Это означает:

> новый кадр добавляет **новое evidence**, а не ещё раз добавляет априорную вероятность страны.

---

# 22. Но соседние Street View кадры нельзя считать независимыми

Например вы стоите на месте и каждые 2 секунды делаете screenshot:

```text
frame1.jpg
frame2.jpg
frame3.jpg
```

между которыми почти ничего не изменилось.

Если каждый раз умножать probability:

```text
Germany .60 × .60 × .60 × .60 × .60 ...
```

через десять кадров система станет практически на 100% уверенной.

Хотя фактически она десять раз увидела **одно и то же доказательство**.

Это ошибка.

---

# 23. Поэтому нужен evidence novelty weight

DINO/SigLIP embedding нового кадра сравниваем с предыдущими:

\[
s_t=\max_j \cos(e_t,e_j).
\]

И рассчитываем:

\[
w_t=f(s_t).
\]

Если изображение почти идентичное:

```text
similarity = .997
w = 0.05
```

Новый ракурс:

```text
similarity = .90
w = .5
```

Вы переместились по дороге, появились совершенно новые объекты:

```text
similarity = .63
w = 1.0
```

Сами пороги я бы **не задавал сейчас**. Их нужно подобрать на вашем sequential benchmark.

---

# 24. Это превращает серию кадров в очень сильный механизм

Представим первый кадр:

```text
сельская дорога
нет знаков
лес

Germany .24
Austria .21
Czechia .18
Slovakia .12
```

Вы двигаетесь.

Второй кадр:

```text
характерные bollards

Austria .43
Czechia .17
Germany .16
```

Дальше:

```text
деревня + дорожный знак

Austria .74
Germany .08
```

Accumulated posterior уже может стать:

```text
Austria        .91
Germany        .035
Czechia        .022
Slovakia       .013
...
```

И это намного ближе к тому, как реально играет человек в GeoGuessr: **он не обязан принимать решение по одному кадру**.

---

# 25. В Chipoint я бы тоже не сохранял только final coordinate

Нужно сохранять:

```text
top 64 candidates
per-tower scores
reranker scores
coordinates
country
admin1
```

Это критически важно.

Потому что если frame 1:

```text
cluster A Germany
cluster B Austria
```

а frame 2:

```text
cluster A Germany
cluster C Switzerland
```

то повторное появление **одного и того же географического cluster A** — очень сильный сигнал.

Если сохранять только:

```text
predicted lat/lon
```

это знание теряется.

---

# 26. То есть temporal fusion должен работать на spatial candidates

Именно:

```text
frame 1:

   ░░ Austria
       █████
      ███████ Germany
       █████


frame 2:

       ░░░░
      ███████ Germany
       █████


combined:

       █████
      █████████
       ███████
```

а страны — только агрегация этого distribution.

---

# 27. Что делать, если пользователь реально перемещается по Street View

Для country/admin-1 ничего страшного.

Для точного spatial posterior можно добавить transition model:

\[
P(g_t)
=
L_t(g_t)
\sum_{g_{t-1}}
T(g_t|g_{t-1})P(g_{t-1}).
\]

`T` разрешает географическое перемещение на небольшое расстояние.

То есть probability cloud может двигаться вместе с пользователем.

Но для первой версии это вообще не требуется — достаточно country/region accumulation.

---

# 28. Google Street View против OSV-5M нужно обязательно вынести в отдельный эксперимент

Это очень существенно.

OSV-5M:

- более 5,1 млн изображений;
- 225 стран и территорий;
- источник — **Mapillary**, не Google;
- train/test пространственно разделены, чтобы модель не решала задачу простым place recognition. :chatgpt-content-reference{index="8"}

Chipoint street gallery тоже построена на OSV-5M. :chatgpt-content-reference{index="9"}

А ваши кадры будут:

```text
Google Street View
```

Поэтому benchmark должен обязательно содержать настоящий GSV.

Причём уже существует небольшой очень полезный dataset для проверки этого эффекта: **OSM-Europe-1k** содержит 1000 Mapillary/OSV locations и координаты соответствующих GSV изображений; удалось получить 837 paired locations. GSV-байты не распространяются, но их предлагается скачивать самостоятельно через Google API. :chatgpt-content-reference{index="10"}

Я бы использовал его как один из первых sanity checks.

---

# 29. Ваш benchmark должен быть устроен ближе к будущему приложению

Не брать:

```text
random image → random train/test
```

Потому что соседние Street View panoramas практически дублируют друг друга.

OSV-5M специально запрещает train image находиться ближе 1 км к test image, чтобы уменьшить place-recognition leakage. :chatgpt-content-reference{index="11"}

Для вашего benchmark я бы делал:

```text
LOCATION
 ├ heading 0°
 ├ heading 90°
 ├ heading 180°
 ├ heading 270°
 ├ moved frame 1
 ├ moved frame 2
 └ moved frame 3
```

и **вся location целиком** должна попадать только в train, validation или test.

Никогда разные views одного места в разные splits.

---

# 30. Метрики тоже нужны не только country accuracy

Я бы считал одновременно примерно такой набор:

| Метрика | Зачем |
|---|---|
| Country top-1 | основной вопрос |
| Country top-3 | насколько модель сохраняет правильную гипотезу |
| Admin-1 top-1 | регион |
| Admin-1 top-3 | качество hierarchy |
| Acc@25 km | локальная точность |
| Acc@200 km | региональная |
| Acc@750 km | грубая страновая |
| Median distance | итоговые координаты |
| NLL | качество вероятностей |
| Brier score | качество probabilistic forecast |
| ECE | calibration |
| Unknown precision | насколько правильно отказывается |
| Frames-to-80% | сколько кадров надо для уверенности |
| Frames-to-90% | то же для строгого режима |

Последние две метрики особенно важны именно **для вашего приложения**.

---

# 31. Самый интересный эксперимент будет не «кто точнее на одном кадре»

А:

```text
                   1 frame   2 frames   3 frames   5 frames   10 frames

Imajev
Chipoint
late fusion
Chip→Jev
learned fusion
```

Я ожидаю, что именно здесь ваша архитектура начнёт выигрывать особенно сильно.

Некоторые методы могут быть одинаковы:

```text
1 frame:
Chipoint 86%
Fusion   87%
```

но:

```text
5 independent views:
Chipoint accumulated 93%
Fusion accumulated   97%
```

Цифры здесь лишь иллюстрация, не прогноз.

---

# 32. Как я бы решал вопрос «какую модель оставить в итоге»

Я бы **не делал выбор частью бизнес-логики**.

Создаём registry:

```text
geo_methods/

imajev_flat
imajev_hier
imajev_semantic

chipoint_raw
chipoint_full

fusion_late
fusion_rerank
fusion_learned
```

Каждый реализует один контракт:

```text
infer(frame, context) → GeoEvidence
```

Затем конфигурация:

```text
active_strategy = fusion_rerank_v3
```

После нового эксперимента:

```text
active_strategy = fusion_learned_v5
```

GUI, temporal accumulator и карта вообще не узнают, что модель поменялась.

---

# 33. Как я бы устроил candidate reranking внутри fusion

Это уже более конкретный вариант, который кажется мне особенно сильным:

```text
IMAGE
  │
  ├───────────────────────────────────────────┐
  │                                           │
  ▼                                           ▼
CHIPOINT                                    IMAJEV
  │                                      direct country
  ▼                                           │
top 64 geographic                             │
candidates                                    │
  │                                           │
  ▼                                           │
aggregate countries                           │
  │                                           │
  └─────────► top 8 countries ◄───────────────┘
                    │
                    ▼
             IMAJEV RERANK
                    │
            Germany       .32
            Austria       .47
            Czechia       .09
            ...
                    │
                    ▼
              FUSION MODEL
                    │
                    ▼
             spatial posterior
```

Это даёт imajev маленькое и реалистичное пространство выбора.

Я подозреваю, что это окажется эффективнее `imajev flat 225`, но именно **подозреваю** — надо измерять.

---

# 34. Ещё лучше — region reranking только после country posterior

Если:

```text
Austria   .47
Germany   .32
Czechia   .09
```

то не надо одновременно давать imajev 4000 административных регионов.

Запрос:

```text
Austria:
Burgenland
Carinthia
Lower Austria
Upper Austria
Salzburg
Styria
Tyrol
Vorarlberg
Vienna

Germany:
только регионы, поддержанные Chipoint
```

И снова сохраняем несколько ветвей.

Это ровно тот случай, где hierarchy даёт огромную вычислительную экономию.

---

# 35. На уровне региона Chipoint должен подсказывать набор вариантов

Например top-64 retrieval имеют массу:

```text
Hessen                .31
Rheinland-Pfalz       .19
Bayern                .14
Baden-Württemberg     .12
Tirol                 .08
Salzburg              .05
...
```

Можно взять top-10 и дать их imajev.

Получаем:

```text
Hessen               .18
Rheinland-Pfalz      .09
Bayern               .07
Baden-Württemberg    .08
Tirol                .41
Salzburg             .12
```

И final fusion может решить, что Tirol становится основным вариантом.

---

# 36. Ещё один важный класс данных — agreement/disagreement

Очень информативно не только то, **что сказали модели**, но и согласились ли они.

Например:

```text
DINO        Austria
SigLIP-A    Austria
SigLIP-B    Austria
imajev      Austria
```

это совсем другая ситуация, чем:

```text
DINO        Austria
SigLIP-A    Germany
SigLIP-B    Slovenia
imajev      Switzerland
```

Во втором случае накопленная heatmap должна оставаться широкой.

Именно поэтому fusion model должен видеть:

```text
tower agreement
model agreement
entropy
geographic dispersion
```

а не только итоговый top-1.

---

# 37. `unknown` я бы сделал частью всей системы

Не только imajev.

Final result может иметь:

```text
P(location evidence sufficient) = .31
```

и:

```text
country:
Germany .16
Austria .14
...
```

То есть система говорит:

> кадр почти не содержит полезных географических признаков.

Такой frame получает маленький temporal weight и почти не изменяет heatmap.

Это намного лучше, чем каждый лесной кадр принудительно голосует за случайную страну.

---

# 38. И именно здесь полезен imajev

У imajev `unknown` — не внешний threshold, а обученный исход; в текущей 4B версии vision tower frozen, поверх Qwen3.5-4B используется LoRA и 255-code decision readout. Авторы отдельно рекомендуют калибровать модель на собственных данных и предоставляют temperature-fitting pipeline. :chatgpt-content-reference{index="12"}

Для нас это означает, что на GSV validation можно отдельно откалибровать:

```text
country questions
region questions
semantic questions
candidate-rerank questions
```

---

# 39. На 3090 Ti я бы ещё заложил возможность отключать части Chipoint

Полный Chipoint использует несколько vision representations и огромную gallery.

Это замечательно для accuracy experiments, но ваша конечная цель — интерактивные 1–2 секунды.

Поэтому интерфейс одного Chipoint expert должен позволять:

```text
towers:
    dino = true
    siglip_gopt = false
    siglip_so400m = false

retrieval:
    exact / ANN

reranker:
    on / off

top_k:
    32 / 64 / 128
```

И тогда benchmark автоматически даст Pareto front:

```text
             accuracy
                ▲
                │          C2 full
                │       ●
                │     ● F1
                │  ● C1
                │●
                └────────────────► latency
```

Не нужно заранее решать, что полный Chipoint обязателен.

---

# 40. Я бы особенно экспериментировал с ANN

Сама Hugging Face-демонстрация Chipoint сейчас держит gallery на диске как `numpy memmap` и последовательно прогоняет по GPU блоками по 250 тыс. строк, чтобы VRAM не росла с размером gallery. :chatgpt-content-reference{index="13"}

Для воспроизведения benchmark это хорошо.

Для интерактивного приложения я бы отдельно проверил:

```text
exact retrieval
vs
FAISS Flat GPU
vs
FAISS IVF
vs
IVF-PQ
```

Потому что вам, возможно, не нужен математически точный top-200 среди 4,89 млн embeddings, если approximate search даёт практически те же страны в 10 раз быстрее.

Это нужно измерить на вашем GSV benchmark.

---

# 41. Важный момент по памяти вашей 3090 Ti

Я бы не строил систему как:

```text
process 1 → Chipoint
process 2 → imajev
```

с двумя независимыми CUDA runtimes, если этого можно избежать.

Лучше иметь:

```text
GeoInferenceWorker
     │
     ├ ChipointModule
     ├ ImajevModule
     ├ OptionalHierLocModule
     └ FusionModule
```

и централизованный GPU scheduler.

Так можно:

```text
encode Chipoint
→ освободить temporary tensors
→ retrieval
→ imajev
→ fusion
```

без ненужной фрагментации VRAM.

Особенно если будут одновременно resident несколько Chipoint towers и 4B imajev.

---

# 42. Что я бы сделал первой рабочей версией

Я бы **не начинал с обученного ensemble**.

Первая версия inference core:

```text
                            IMAGE
                              │
                 ┌────────────┴─────────────┐
                 ▼                          ▼
          Chipoint street              imajev-4b
                 │                          │
            top64 coords            flat country
                 │                   + unknown
                 │                          │
                 └────────────┬─────────────┘
                              ▼
                    probability calibration
                              │
                              ▼
                       spatial late fusion
                              │
                              ▼
                         FRAME POSTERIOR
                              │
                              ▼
                       temporal updater
                              │
                              ▼
                  accumulated posterior
```

И параллельно доступен второй imajev режим:

```text
Chipoint top countries
        ↓
imajev candidate reranker
        ↓
fusion
```

То есть уже первая версия даст **четыре содержательных сравнения**:

```text
imajev
Chipoint
independent fusion
Chipoint→imajev
```

---

# 43. После накопления результатов — learned fusion

Когда появятся тысячи GSV examples, добавляем:

```text
FusionV2
```

без изменения остальных компонентов.

Он обучается только на записанных `GeoEvidence`.

То есть вам даже **не потребуется повторно запускать GPU models**, чтобы экспериментировать с новой формулой fusion.

Это очень важная деталь архитектуры.

Для каждого benchmark frame надо сохранять:

```text
ground truth
+
raw Chipoint output
+
raw imajev logits
+
semantic outputs
+
embeddings diagnostics
```

И тогда можно неделями экспериментировать с fusion offline за минуты.

---

# 44. Я бы разделил inference и decision полностью

Это означает:

```text
EXPENSIVE STAGE

image
 ↓
model inference
 ↓
immutable raw evidence
 ↓
SAVE
```

а затем:

```text
CHEAP STAGE

raw evidence
 ↓
calibration v1 / v2 / v3
 ↓
fusion v1 / v2 / v3
 ↓
temporal aggregation v1 / v2 / v3
 ↓
metrics
```

Это позволит честно сравнивать десятки алгоритмов на **абсолютно одинаковых model outputs**.

Не придётся гонять 3090 Ti заново после каждого изменения формулы.

---

# 45. Мой итоговый candidate architecture

Если сейчас зафиксировать наиболее перспективную схему, я бы выбрал такую:

```text
                         GOOGLE STREET VIEW FRAME
                                   │
                                   ▼
                         shared preprocessing
                                   │
             ┌─────────────────────┴────────────────────┐
             │                                          │
             ▼                                          ▼
       CHIPOINT STREET                             IMAJEV-4B
             │                                          │
   DINO + SigLIP retrieval                    direct country head
             │                                semantic geo heads
         reranker                                unknown head
             │                                          │
             ▼                                          │
     top spatial candidates                             │
             │                                          │
             ├───── aggregate country/admin1 ───────────┤
             │                                          │
             ▼                                          │
    Chipoint candidate list ───────► imajev reranker ◄──┘
             │                           │
             └──────────────┬────────────┘
                            ▼
                    calibrated evidence
                            │
                    ┌───────┴────────┐
                    │                │
                    ▼                ▼
               late fusion      learned fusion
                    │                │
                    └───────┬────────┘
                            ▼
                    spatial posterior
                            │
                       geo-cell map
                            │
                ┌───────────┼──────────┐
                ▼           ▼          ▼
             country      admin1    coordinates
                            │
                            ▼
                    temporal evidence
                       accumulator
                            │
                            ▼
                 FINAL PROBABILITY MAP
```

И **HierLoc/GeoSURGE-подобный эксперт можно добавить позже просто третьей ветвью**, не меняя всё остальное.

---

## Что я считаю наиболее вероятным результатом экспериментов

Я бы **не ставил сейчас на чистый imajev** как итоговое решение. Он слишком общий.

Я бы также не предполагал, что чистый Chipoint обязательно победит на Google Street View, потому что его street gallery построена на Mapillary/OSV-5M, а ваш production domain другой. :chatgpt-content-reference{index="14"}

Наиболее перспективным для меня выглядит:

**Chipoint как географический candidate generator → imajev-4b как semantic candidate reranker → откалиброванный late/learned fusion → накопление spatial posterior по нескольким независимым кадрам.**

Причём **накопление нескольких GSV кадров может оказаться более важным улучшением, чем замена одной сильной single-frame модели на другую**. Большинство опубликованных benchmark-цифр относятся к одному изображению; ваше приложение может использовать последовательное evidence и постепенно сужать распределение.

И ещё важнее: я бы **вообще не заставлял систему окончательно выбирать страну после каждого кадра**. Её естественное состояние должно выглядеть как:

```text
ROUND STATE

Austria          0.47
Germany          0.31
Switzerland      0.08
Slovenia         0.06
Czechia          0.04
Other            0.04

Admin-1:
Tirol            0.21
Salzburg         0.16
Bayern           0.12
Vorarlberg       0.08
...

Spatial posterior:
1 284 active cells

Evidence:
8 screenshots
4.3 effective independent frames
```

А очередной скриншот просто **деформирует это вероятностное поле**. Именно под такую модель состояния я бы проектировал ядро.
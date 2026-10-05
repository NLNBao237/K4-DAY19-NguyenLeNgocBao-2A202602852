# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Nguyễn Lê Ngọc Bảo  **MSSV:** 2A202602852  **Ngày:** 06-10-2026

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Mọi số liệu khớp với `ket_qua_benchmark_kg.txt`. Bản thiết kế ontology nộp riêng ở `report/ONTOLOGY.md` (ontology tự thiết kế; baseline ontology gợi ý ở `ket_qua_benchmark_kg.hint.txt`).

## 1. Chi phí (10 điểm)

```
Chat model: openai:gpt-4o-mini | Embedding: openai:text-embedding-3-small | top_k=3 | chunk_size=800 | chunks=176 | KG: 339 nodes / 624 rels

== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176     56072        0   0.00112    117.1
graph       196     99758     5921   0.01123    248.4

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.43   1.00      694       46   0.00012     2.62
graph       1.00   2.00     2135       80   0.00036     3.94
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | 0,00112 | 0,01123 | ×10,0 |
| Indexing giây | 117,1 | 248,4 | ×2,1 |
| Mỗi câu: USD | 0,00012 | 0,00036 | ×3,0 |
| Mỗi câu: giây | 2,62 | 3,94 | ×1,5 |
| Mỗi câu: in_tok | 694 | 2135 | ×3,1 |

**Chi phí tăng thêm đến từ đâu?**

> **Indexing:** phần chênh là đúng 20 lần gọi LLM trích xuất tin tức (196 − 176 call): 43 686 token vào + 5 921 token ra = 0,01011 USD và 131 giây, tức 90% chi phí dựng GraphRAG. Phần luật (18 Điều → 99 khoản, 116 ngưỡng khối lượng) trích bằng regex nên tốn 0 token. Token ra tuy chỉ bằng 14% token vào nhưng chiếm 35% tiền trích xuất (0,00355 / 0,01011 USD) vì giá token ra gấp 4.
>
> **Mỗi câu hỏi:** vector search giống hệt nhau, nên toàn bộ phần thêm là dữ kiện graph chèn vào prompt: trung bình +1 441 token vào (+0,00024 USD) và +1,3 giây (các truy vấn Cypher trong `context()` + prompt dài hơn). Mức thêm phụ thuộc loại câu: Q1 (luật, 1 bước) graph trả 0 dữ kiện nên gần như không thêm (0,00012 → 0,00013 USD), còn Q6 (tổng hợp) thêm nhiều nhất (0,00016 → 0,00069 USD).
>
> **Điểm hòa vốn:** KG không bao giờ "rẻ hơn" Flat; nó mua độ chính xác. Hai cách nhìn: (1) phí dựng 0,01011 USD bằng phụ phí của khoảng 42 câu hỏi (0,01011 / 0,00024), nên từ vài chục câu trở lên thì chi phí chính là prompt dài hơn mỗi câu chứ không phải phí dựng; (2) tính theo **câu trả lời đúng đủ** (judge = 2): Flat đúng 2/6 câu, Graph đúng 6/6, nên chi phí truy vấn cho mỗi câu đúng xấp xỉ nhau: Flat 0,00074 / 2 = 0,00037 USD; Graph 0,00217 / 6 = 0,00036 USD (tổng cột USD của 6 dòng mỗi pipeline khi chạy benchmark). Trên bộ 6 câu này, GraphRAG đắt gấp 3 mỗi câu nhưng không đắt hơn mỗi câu **đúng**. Cảnh báo: chỉ 6 câu, mỗi cấu hình chạy 1 lần.

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1,00 / 2 | 1,00 / 2 | Hòa (Flat rẻ hơn chút) | Định nghĩa "tiền chất" nằm gọn trong một khoản của Điều 2 Luật PCMT; graph không có gì để thêm và cũng không thêm gì. |
| Q2 | single-hop-news | 1,00 / 2 | 1,00 / 2 | Hòa (Flat rẻ hơn 3,4 lần) | Hai bị cáo lãnh án tử hình nằm trong cùng một bài báo; dữ kiện graph chỉ lặp lại chunk, tốn 0,00051 so với 0,00015 USD. |
| Q3 | cross-kb | 0,00 / 0 | 1,00 / 2 | Graph | Flat trả lời "Không đủ thông tin": top-3 chunk không có đoạn nào chứa cả mức án lẫn Điều 251; graph đi Person → Crime → Article. |
| Q4 | cross-kb | 0,00 / 0 | 1,00 / 2 | Graph | Câu hỏi chỉ dùng biệt danh "Hoàng Nato"; graph khớp alias → Dương Minh Tuấn → tội danh riêng → `max_penalty` của Điều 255. |
| Q5 | cross-kb-multi-hop | 0,60 / 1 | 1,00 / 2 | Graph | Flat không nêu được Điều 250 và viết sai "khoản b"; graph so 9600 g với ngưỡng và trả thẳng "điểm b khoản 4 Điều 250". |
| Q6 | aggregation | 0,00 / 1 | 1,00 / 2 | Graph | Flat chỉ thấy 3 chunk và gọi vụ bằng tên riêng một người ("vụ của Đức", "vụ của Thành"); graph liệt kê mọi `Case` có cạnh `INVOLVES` tới MDMA. |

**Quy luật:** bên thắng phụ thuộc vào việc đáp án nằm trong **một** đoạn văn hay không. Câu 1 bước (Q1, Q2): hòa về chất lượng, Flat thắng về giá. Câu phải ghép hai KB (Q3, Q4, Q5) hoặc phải gom nhiều tài liệu (Q6): Flat 0–1 điểm judge, Graph 2 điểm ở cả 4 câu. Recall các câu `cross-kb`: Flat 0,00 / 0,00 / 0,60, Graph 1,00 / 1,00 / 1,00, đúng kỳ vọng định tính của đề.

## 3. Phân tích lỗi (20 điểm)

Các truy vấn dưới đây chạy trên graph của `ket_qua_benchmark_kg.txt` (339 node / 624 cạnh), đã đổi label/quan hệ theo ontology của tôi. Điểm tổng 1,00 / 2,00 không có nghĩa là graph sạch: cả 4 lỗi dưới đây đều tồn tại trong graph đó, chỉ là 6 câu hỏi không chạm tới.

### Lỗi E3: Trùng thực thể (một vụ ngoài đời thành hai `Case`) và mặt trái của việc gộp

- **Hiện tượng:** vụ Viện Pháp y tâm thần Trung ương xuất hiện thành 2 node `Case`. Node thứ hai không có người, không có tội danh, không có chất. Ngược lại, vụ Hoàng Nato được gộp từ 5 bài, trong đó có một bài nói về chuyện khác.
- **Bằng chứng:**

```cypher
MATCH (k:Case)
RETURN k.name AS name, size([(s:Source)-[:REPORTS]->(k)|s]) AS sources,
       size([(p)-[:INVOLVED_IN]->(k)|p]) AS people
ORDER BY sources DESC, name
```

```
"Vụ bắt giang hồ 'Hoàng Nato' và 126 người liên quan 8 đường dây ma túy"  sources 5  people 9
"Vụ bắt giữ Nguyễn Minh Đức tại Ninh Bình"                                 sources 2  people 1
"Vụ vận chuyển ma túy từ Đức về Việt Nam"                                  sources 2  people 3
"Vụ án tại Viện Pháp y tâm thần Trung ương"                                sources 2  people 10
"Vụ tiệc ma túy trên bãi biển Sầm Sơn"                                     sources 1  people 0
... (12 dòng; 5 vụ có people = 0)
```

  Node "Vụ tiệc ma túy trên bãi biển Sầm Sơn" đến từ `news-100260924101703641` (bài về "Đức Cộng"). Đoạn cuối bài đó là tin liên quan: *"Từ vụ bắt quả tang một "tiệc" ma túy trên bãi biển Sầm Sơn, cơ quan điều tra mở rộng vụ án liên quan những sai phạm đưa, nhận hối lộ làm sai lệch kết quả giám định tâm thần tại Viện Pháp y tâm thần Trung ương."* Tức là cùng một vụ.

  Chiều ngược lại: 5 nguồn của vụ Hoàng Nato gồm cả `news-100260927182621527` (bài "Biên phòng phát hiện bao tải… nghi chứa 20kg ma túy" ở Phú Quốc), chỉ vì đoạn cuối bài này nhắc *"Trong số 126 người bị… Công an TP.HCM bắt… có TikToker Phannhibeauty"*.
- **Nguyên nhân:** hai tầng.
  1. **Crawl:** `scripts/crawl_drug_corpus.py` giữ lại đoạn "tin liên quan" ở cuối mỗi bài, nên một file `.md` chứa mẩu tin của vụ khác.
  2. **Thiết kế ontology:** khóa của `Case` trong thiết kế của tôi dựa trên tên bị can/bị cáo chung (`resolve_cases`). Mẩu tin liên quan dài 2 câu không nêu tên ai, nên không có gì để gộp và nó thành một `Case` mồ côi. Khi mẩu tin **có** nêu tên (Phannhibeauty) thì gộp đúng vụ, nhưng cạnh `REPORTS` lại nói bài Phú Quốc "đưa tin về" vụ Hoàng Nato, làm nhiễu bước chọn vụ theo `doc_id` trong `context()`.

  So với ontology gợi ý thì đã tốt hơn (baseline, đo trên một lần dựng riêng, xem `ONTOLOGY.md` mục 7: 4 `Case` khác tên cho vụ Hoàng Nato, 14 `Case` tổng; của tôi: 1 và 12), nhưng chưa hết.
- **Đề xuất sửa:**
  1. Sửa tận gốc ở `scripts/crawl_drug_corpus.py`: cắt khối tin liên quan (thẻ related của trang) trước khi ghi file. Không tốn token; giảm cả token trích xuất. Đánh đổi: phải crawl lại và chạy lại benchmark, số liệu Flat cũng đổi theo.
  2. Trong `resolve_cases` (`src/graph.py`): với `Case` không có người, gộp theo khóa phụ (địa điểm + chất + ngày), hoặc bỏ hẳn `Case` không có người, không tội danh, không chất. Đánh đổi: khóa phụ dễ gộp nhầm hơn tên người; bỏ node thì mất các vụ thật chưa lộ danh tính (vụ 20kg dạt bờ ở Phú Quốc).
  3. Thêm property trên cạnh `REPORTS` (`main: true/false`) để `context()` chỉ chọn vụ là chủ đề chính của bài. Tốn thêm vài token đầu ra mỗi bài.

### Lỗi E2: Thiếu ngữ cảnh luật (graph không xác định được khoản cho phần lớn vụ án)

- **Hiện tượng:** ontology của tôi tính được khoản áp dụng cho Q5 (9,6kg MDMA → khoản 4 Điều 250), nhưng với đa số vụ khác thì không: chỉ 6/15 cạnh `INVOLVES` có khối lượng quy đổi được ra gam. Vụ lớn nhất trong benchmark (Q2, hơn 36kg, hai án tử hình) không có ngưỡng nào khớp.
- **Bằng chứng:**

```cypher
MATCH (:Case)-[i:INVOLVES]->(:Substance)
RETURN count(*) AS involves, sum(CASE WHEN i.grams IS NULL THEN 0 ELSE 1 END) AS with_grams
```

```
involves: 15, with_grams: 6
```

```cypher
MATCH (k:Case)-[i:INVOLVES]->(s) WHERE k.name CONTAINS '36kg'
RETURN k.name, s.name, i.amount,
       size([(t:Threshold)-[:OF_SUBSTANCE]->(s) | t]) AS thresholds
```

```
"Vụ mua bán hơn 36kg ma túy tại TP.HCM"  "ma túy (không rõ loại)"  "hơn 36kg"  thresholds 0
```

```cypher
MATCH (k:Case)-[i:INVOLVES]->(:Substance {name:'MDMA'}) RETURN k.name, i.amount, i.grams
```

```
"Vụ án tại Viện Pháp y tâm thần Trung ương"   "0,686g"                                0.686
"Vụ bắt giang hồ 'Hoàng Nato' ..."            "khoảng 100g ma túy tổng hợp các loại"  null
"Vụ góp tiền mua ma túy tại TP Hà Nội"        "5 viên nén"                            null
"Vụ vận chuyển ma túy từ Đức về Việt Nam"     "hơn 9,6kg"                             9600.0
```

  Hệ quả nhìn thấy trong file kết quả: câu trả lời Q2 (graph) nêu đúng hai bị cáo nhưng graph không cung cấp được căn cứ "khoản 4 Điều 251" cho án tử hình; câu hỏi không hỏi nên điểm vẫn 2. Nếu hỏi "vì sao tử hình, theo khoản nào" thì graph chỉ đưa được thang hình phạt của Điều 251, không kết luận được.

  Đây cũng chính là chỗ quy tắc lọc khoản của ontology gợi ý bỏ sót, ở dạng nặng hơn. Q4 trong `ket_qua_benchmark_kg.hint.txt`: *"Hành vi này có thể bị phạt tù tối đa 7 năm theo Điều 255 BLHS"* (recall 0,67, judge 1). Quy tắc "khoản 1 + khoản nhắc tới chất của vụ" chỉ trả về khoản 1, vì không khoản nào của Điều 255 nhắc tên chất:

```cypher
// chạy trên graph baseline (KG_ONTOLOGY=hint)
MATCH (p:Person)-[:INVOLVED_IN]->(k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article {id:'Điều 255 BLHS'})-[:HAS_CLAUSE]->(cl:Clause)
WHERE 'Hoàng Nato' IN p.aliases AND (cl.number = 1 OR EXISTS { (k)-[:INVOLVES]->(:Substance)<-[:MENTIONS]-(cl) })
RETURN DISTINCT a.id, cl.number, cl.penalty
```

```
"Điều 255 BLHS"  1  "phạt tù từ 02 năm đến 07 năm"
```

- **Nguyên nhân:**
  1. **Dữ liệu nguồn:** báo ghi khối lượng theo đơn vị không quy đổi được ("5 viên", "nửa chỉ", "1.000 đầu pod") hoặc không nêu loại chất ("36kg ma túy").
  2. **Thiết kế ontology:** `Threshold` chỉ mô hình hóa điểm có ngưỡng khối lượng của chất gọi tên. Chưa có điểm "có 02 chất ma túy trở lên… tổng khối lượng tương đương", và chưa có tình tiết định khung phi khối lượng (có tổ chức, tái phạm nguy hiểm…).
  3. **Quyết định có chủ đích trong code:** khối lượng dùng chung cho nhiều chất bị để `grams = null` (`extract_news_cases_own`) để không sinh kết luận sai. Tôi chọn thiếu thay vì sai.
- **Đề xuất sửa:**
  1. Với chất `ma túy (không rõ loại)`: trong `_context_own`, thêm một dòng dữ kiện có điều kiện, liệt kê nhóm chất nào thì khối lượng đó đã vượt ngưỡng khoản cao nhất (ví dụ 36 000 g vượt ngưỡng khoản 4 Điều 251 nếu là Heroine/Methamphetamine/MDMA (≥ 100 g) hoặc chất rắn khác (≥ 300 g), nhưng chưa vượt nếu là quả thuốc phiện khô (≥ 600 kg)). Thêm khoảng 40 token mỗi vụ; rủi ro là LLM đọc câu có điều kiện thành kết luận chắc chắn.
  2. Mô hình hóa tình tiết định khung: node `Circumstance` nối `Clause` (regex trên các điểm không có số lượng) và để LLM trích tình tiết của vụ. Trả lời được "vì sao khoản 2", nhưng thêm một trường vào prompt trích xuất (tốn token ra, loại đắt nhất) và thêm một chỗ LLM có thể bịa.
  3. Không nên quy đổi "viên" ra gam bằng hệ số ước lượng: sai số lớn mà kết luận pháp lý lại nhạy với ngưỡng.

### Lỗi E4: Phép đo sai (recall và judge mâu thuẫn; judge không ổn định)

- **Hiện tượng:** (a) Q6 Flat có recall 0,00 nhưng judge 1. (b) Cùng pipeline Flat, cùng câu Q5, cùng recall 0,60, nhưng judge cho 2 ở lần chạy baseline và 1 ở lần chạy chính. (c) Q5 và Q6 Graph được judge 2 dù thiếu ý so với đáp án chuẩn / so với graph.
- **Bằng chứng:**
  - (a) `ket_qua_benchmark_kg.txt`, Q6 flat (recall=0.00 judge=1): *"1. Vụ việc của Đức liên quan đến số viên nén hình tam giác màu hồng - xám là MDMA. 2. Vụ việc của Thành liên quan đến 5 viên nén màu trắng… 3. Vụ việc của Đông cũng liên quan đến 0,686g ma túy MDMA."* `must_include` là `["Cái Quang Huy", "Lê Minh Thành", "Pháp y tâm thần"]`. Câu trả lời chỉ đúng 3 vụ nhưng gọi bằng tên riêng ("Thành", "Đông") nên không khớp chuỗi nào.
  - (b) Dòng tiêu đề trong hai file: `--- Q5 [cross-kb-multi-hop] flat recall=0.60 judge=2 3.35s` (file `.hint.txt`) và `--- Q5 [cross-kb-multi-hop] flat recall=0.60 judge=1 2.56s` (file chính). Pipeline Flat không phụ thuộc ontology; câu trả lời ở lần chính có lỗi rõ: *"khoản b của điều luật tương ứng được áp dụng"* (không có "khoản b"; đúng là điểm b khoản 4) và không nêu Điều 250.
  - (c) Q5 graph (judge=2): *"…với loại ma túy là MDMA"*, không nhắc Ketamine, trong khi đáp án chuẩn có *"hơn 9,6kg MDMA và khoảng 406g Ketamine"*. `must_include` không có "Ketamine" nên recall vẫn 1,00.
  - Thêm một lỗi của phép đo thời gian: trong `ket_qua_benchmark_kg.hint.txt`, `Q2 flat … 93.54s` (một lần gọi API bị treo) kéo trung bình Flat lên 17,82 giây, khiến Flat trông chậm gấp 5 lần Graph (3,50 giây). Lần chạy chính không gặp: Flat 2,62 giây.
- **Nguyên nhân:** nằm ở **phép đo** (`bench_kg.py` + `data/benchmark_kg.json`), không phải ở pipeline.
  - `keyword_recall` so khớp chuỗi con nguyên văn: không hiểu "Thành" = "Lê Minh Thành", và không phạt ý sai ("khoản b").
  - Judge là LLM chấm một lần, không có rubric theo từng ý, nên cho điểm khác nhau cho hai câu trả lời gần giống nhau và dễ dãi với câu thiếu ý phụ.
  - Trung bình cộng thời gian trên 6 mẫu rất nhạy với một giá trị ngoại lai.
  - Ở (a), tôi cho rằng **judge đúng hơn recall** (câu trả lời đúng một phần). Ở (b), judge = 1 của lần chạy chính hợp lý hơn judge = 2.
- **Đề xuất sửa:** (không sửa `bench_kg.py` trong bài nộp vì đề cấm; đây là đề xuất)
  1. `must_include` cho phép biến thể: mỗi phần tử là một danh sách đồng nghĩa (`["Lê Minh Thành", "vụ của Thành"]`), và thêm `must_not_include` cho lỗi điển hình. Không tốn token.
  2. Judge theo rubric: tách đáp án chuẩn thành từng ý, chấm có/không cho mỗi ý rồi cộng. Ổn định hơn, nhưng prompt chấm dài hơn khoảng 2 lần.
  3. Chạy mỗi cấu hình ít nhất 3 lần và báo trung vị cho thời gian. Tốn gấp 3 chi phí benchmark (vẫn dưới 0,1 USD).

### Lỗi E5: LLM lệch với graph (câu tổng hợp bỏ sót một vụ có trong dữ kiện)

- **Hiện tượng:** Q6 hỏi các vụ liên quan MDMA. Graph có 4 vụ và `context()` đưa cả 4 vào prompt, nhưng câu trả lời GraphRAG chỉ liệt kê 3, bỏ vụ Hoàng Nato. Điểm vẫn tối đa (recall 1,00, judge 2) vì đáp án chuẩn cũng chỉ có 3 vụ.
- **Bằng chứng:** Cypher trả lời thẳng câu hỏi:

```cypher
MATCH (k:Case)-[i:INVOLVES]->(:Substance {name:'MDMA'}) RETURN k.name AS case, i.amount AS amount
```

```
"Vụ án tại Viện Pháp y tâm thần Trung ương"                               "0,686g"
"Vụ bắt giang hồ 'Hoàng Nato' và 126 người liên quan 8 đường dây ma túy"   "khoảng 100g ma túy tổng hợp các loại"
"Vụ góp tiền mua ma túy tại TP Hà Nội"                                     "5 viên nén"
"Vụ vận chuyển ma túy từ Đức về Việt Nam"                                  "hơn 9,6kg"
```

  Câu trả lời Q6 graph trong file kết quả: *"1. Vụ góp tiền mua ma túy tại TP Hà Nội… 2. Vụ vận chuyển ma túy từ Đức về Việt Nam… 3. Vụ án tại Viện Pháp y tâm thần Trung ương… Tất cả các vụ việc này đều có liên quan đến ma túy MDMA."* Thiếu vụ Hoàng Nato. Vụ này có cơ sở trong nguồn: `news-100260920221957595` viết *"…mua bán ma túy loại etomidate, ketamine, thuốc lắc và các loại ma túy tổng hợp"* ("thuốc lắc" được `link_substance` nối về `MDMA`).
- **Nguyên nhân:** hai bước.
  1. **Prompt trả lời / LLM:** `GRAPH_PROMPT` không yêu cầu liệt kê **đủ** mọi dữ kiện khớp. Ba vụ kia được 3 chunk vector nhắc lại (có chữ "MDMA" nguyên văn), còn vụ Hoàng Nato chỉ có trong dữ kiện graph và dòng dữ kiện của nó ghi *"khoảng 100g ma túy tổng hợp các loại"* chứ không ghi "MDMA" cạnh con số, nên LLM coi là yếu và bỏ.
  2. **Phép đo:** đáp án chuẩn của Q6 thiếu vụ này, nên lỗi không bị phát hiện bằng điểm số. Nếu đọc điểm thay vì đọc câu trả lời thì không thấy.

  Tôi chưa chạy lại benchmark lần hai để xem lỗi có lặp lại y hệt không (mỗi lần chạy dựng lại graph nên tên vụ và dữ kiện cũng thay đổi); với `temperature=0` cùng một prompt thường cho cùng kết quả, nhưng prompt ở lần sau sẽ không còn giống.
- **Đề xuất sửa:**
  1. Với câu tổng hợp, để graph trả lời trực tiếp: `_context_own` thêm dòng mở đầu "Graph có đúng 4 vụ việc liên quan MDMA:" và thêm vào `GRAPH_PROMPT` câu "liệt kê đủ mọi vụ việc trong dữ kiện knowledge graph". Thêm khoảng 30 token mỗi câu; rủi ro là LLM liệt kê cả vụ do trích xuất sai.
  2. Ghi rõ căn cứ trên cạnh: `INVOLVES {evidence: "…thuốc lắc…"}` để dòng dữ kiện nêu được vì sao vụ đó có MDMA. Tốn thêm token ra khi trích xuất.
  3. Bổ sung vụ Hoàng Nato vào `gold` và `must_include` của Q6 (hoặc ghi chú là không tính), nếu không phép đo sẽ tiếp tục thưởng cho câu trả lời thiếu.

## 4. Kết luận (5 điểm)

> **Flat RAG là đủ** khi đáp án nằm trong một đoạn văn của một tài liệu: Q1 và Q2 đều đạt recall 1,00 / judge 2 ở cả hai pipeline, và Flat rẻ hơn (0,00012–0,00015 USD so với 0,00013–0,00051 USD mỗi câu), nhanh hơn (2,62 so với 3,94 giây trung bình), không tốn 0,01011 USD và 131 giây dựng graph.
>
> **Nên dùng KG** khi thỏa cả ba điều kiện:
> 1. **Loại câu hỏi:** phải ghép dữ kiện từ nhiều nguồn (Q3–Q5: Flat recall 0,00 / 0,00 / 0,60, Graph 1,00 cả ba), phải tính toán trên cấu trúc (Q5: so khối lượng với ngưỡng), hoặc phải gom đủ mọi trường hợp (Q6: Flat recall 0,00, Graph 1,00). Trên 4 câu này Flat không có câu nào đạt judge 2, Graph đạt cả 4.
> 2. **Loại dữ liệu:** có ít nhất một nguồn cấu trúc đều để trích bằng regex (luật: 0 token, lặp lại được) và một khái niệm chung làm cầu nối (tội danh, chất). Nếu cả hai KB đều là văn xuôi thì phí trích xuất LLM tăng gấp đôi và lỗi trích xuất (mục 3) nhân lên.
> 3. **Số lượng câu hỏi:** phí dựng (0,01011 USD) tương đương phụ phí của khoảng 42 câu; dữ liệu đổi thường xuyên mà chỉ hỏi vài câu thì không đáng dựng. Còn khi hỏi nhiều, chi phí cần cân là +0,00024 USD và +1,3 giây mỗi câu, đổi lấy judge 1,00 → 2,00.
>
> **Thiết kế ontology quan trọng hơn việc "có graph hay không":** cùng dữ liệu, cùng model, ontology gợi ý cho GraphRAG recall 0,83 / judge 1,67 với 3704 token mỗi câu; ontology tự thiết kế cho 1,00 / 2,00 với 2135 token (rẻ hơn 39% mỗi câu, đắt hơn 21% lúc dựng). Graph tính sẵn được điều gì (ngưỡng khối lượng, khung cao nhất) thì LLM không phải đọc nguyên văn luật để tự suy ra.
>
> **Giới hạn của kết luận:** 6 câu hỏi, mỗi cấu hình chạy 1 lần, judge là LLM và không ổn định (lỗi E4); graph vẫn còn lỗi trùng thực thể và thiếu khối lượng (E3, E2) mà bộ câu hỏi không chạm tới. Con số 1,00 / 2,00 là kết quả trên bộ này, không phải độ chính xác kỳ vọng nói chung.

## 5. Tự kiểm (5 điểm)

```
$ pytest tests/ -q
................................................                         [100%]
48 passed in 0.06s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = openai:gpt-4o-mini | embedding = openai:text-embedding-3-small
[OK] KG-2 build_graph: 272 node / 520 cạnh, đường xuyên 2 KB dài 3 cạnh
[OK] KG-3 context: 14 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00089. Graph nhỏ (luật + 1 bài) vẫn còn trong Neo4j để bạn xem; chạy --judge để dựng graph đầy đủ.
```

Ảnh Neo4j: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`.

Truy vấn dùng cho từng ảnh (đã đổi theo ontology của tôi):

```cypher
// kg_count.png (Q-A)
MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY n DESC;

// kg_cross_kb.png (Q-B): bài báo -> vụ việc -> tội danh (cầu nối) <- Điều luật
MATCH p=(:Source)-[:REPORTS]->(:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(:Article) RETURN p LIMIT 50;

// kg_my_case.png (Q-D): người -> vụ -> tội -> Điều, kèm ngưỡng khối lượng khớp với tang vật
MATCH p=(:Person {name:'Cái Quang Huy'})-[:INVOLVED_IN]->(k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)
OPTIONAL MATCH q=(k)-[i:INVOLVES]->(:Substance)<-[:OF_SUBSTANCE]-(t:Threshold)<-[:HAS_THRESHOLD]-(:Clause)<-[:HAS_CLAUSE]-(a)
  WHERE t.min_qty <= i.grams AND (t.max_qty IS NULL OR i.grams < t.max_qty)
OPTIONAL MATCH r=(k)-[:INVOLVES|LOCATED_IN]->()
OPTIONAL MATCH s=(:Source)-[:REPORTS]->(k)
RETURN p, q, r, s;
```

Người đã chọn cho `kg_my_case.png`: **Cái Quang Huy** (vụ vận chuyển ma túy từ Đức về Việt Nam; ảnh cho thấy cả đường MDMA → Threshold → Clause → Điều 250).

## Vấn đề gặp phải (không tính điểm)

> - **LLM không tuân thủ prompt trích xuất.** `gpt-4o-mini` trả nguyên chữ `"chuỗi rỗng"` làm giá trị, ghi chức danh ("cựu viện trưởng", "điều dưỡng") vào trường `role`, tự đặt tên người ("Người Trung Quốc 1" … "7"), và gán cùng một khối lượng tổng cho nhiều chất. Thử thêm quy tắc vào prompt thì hết lỗi này lại sinh lỗi khác (thêm quy tắc "khối lượng tổng thì ghi vào chất 'ma túy'" làm vụ Lê Minh Thành mất luôn MDMA). Cách giải quyết cuối cùng: giữ prompt ngắn, xử lý trong code (`clean_text`, `normalize_role`, `PLACEHOLDER_PERSON`, phát hiện khối lượng dùng chung trong `extract_news_cases_own`).
> - **Mỗi lần dựng graph cho kết quả hơi khác** (339 node ở lần benchmark, 343–348 ở các lần `--build` thử trước đó) dù `temperature=0`. Vì vậy ảnh chụp và các truy vấn ở mục 3 đều lấy từ đúng graph của lần chạy `--judge` cuối, không dựng lại sau đó.
> - **Một lần gọi API bị treo 93 giây** ở lần chạy baseline (Q2 flat), làm lệch cột `seconds` của Flat trong `ket_qua_benchmark_kg.hint.txt`; xem lỗi E4.
> - Ảnh Neo4j Browser được chụp tự động bằng trình duyệt không giao diện (cửa sổ 1600×900), nên không có thanh địa chỉ của trình duyệt; ô truy vấn và Results overview vẫn thấy đủ.

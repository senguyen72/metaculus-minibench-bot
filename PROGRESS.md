# Metaculus MiniBench — PROGRESS

Tài khoản: senguyen72 (senguyen72@gmail.com) · Bot: **senguyen72-bot**
(bot đầu tiên trên tài khoản — bot duy nhất đủ điều kiện nhận giải MiniBench).
Token lưu trong `config.json` (chmod 600). Không ghi token ra đây.

## 23/11/2026 — Bot baseline v1 XONG

### Đã đọc docs
- Spec OpenAPI chính thức v2.0.0 (`openapi.yml`, tải từ
  `raw.githubusercontent.com/Metaculus/metaculus/main/docs/openapi.yml`).
- Endpoints dùng: `GET /api/posts/` (lọc `tournaments=minibench`,
  `statuses=open`, `with_cp=true`, `include_descriptions=true`),
  `GET /api/posts/{id}/`, `POST /api/questions/forecast/`
  (trả 201 khi nộp thành công; 400 sai format; 403 không được phép;
  405 câu đã đóng). Auth: header `Authorization: Token <token>`.
- Forecast theo `question.id` (không phải `post.id`).
- Quy tắc CDF theo spec: 201 điểm (hoặc `inbound_outcome_count + 1` cho
  discrete), bước tối thiểu 0.01/inbound, bước tối đa 0.2·(200/inbound),
  bound kín/mở (cdf[0]=0 / ≥0.001, cdf[-1]=1 / ≤0.999). Đã port hàm
  `generate_continuous_cdf` + `standardize_cdf` của spec sang stdlib
  (không dùng numpy) và kiểm tra lại bằng `validate_cdf` trước mỗi lần nộp.

### Đã viết
- `bot.py` (stdlib only, không tốn tiền, không LLM, không dịch vụ trả phí):
  lấy câu hỏi MiniBench đang mở → build forecast → nộp (hoặc dry-run).
  Logic forecast (ghi trong docstring của code):
  - Neo = Community Prediction (CP) khi API trả về: binary co 15% về 0.5,
    MC co 15% về đều, numeric/discrete/date lấy median CP làm tâm, sd 25%.
  - Fallback khi CP null: binary 0.5, MC đều, continuous CDF chuẩn rộng,
    tâm = giữa khoảng (giữa nhân cho log-scale), sd = 40% khoảng.
  - Bỏ qua câu đã forecast (kiểm tra qua post-detail; xem dưới).
  - Ghi state vào `state.json` để không nộp trùng.
  - Retry 5 lần, timeout cứng 25s bằng SIGALRM (xem mục lỗi).
- `.github/workflows/forecast.yml`: chạy `bot.py --submit` mỗi 6 giờ
  (cron `0 */6 * * *`) + chạy tay được; commit `state.json` về repo.
  Cần: repo GitHub + secret `METACULUS_TOKEN` + quyền workflow read/write.
  (Repo chưa tạo — việc tạo repo/push + thêm secret để main agent làm,
  cần trình duyệt/tài khoản GitHub.)
- `README.md`, `.gitignore` (config.json không bao giờ commit).

### Kiểm tra đã chạy
1. `--selftest` (offline): **PASS** — 4 case CDF (kín/mở, linear/log,
   discrete 10 bucket) đều hợp lệ; binary/MC fallback đúng; neo CP
   0.8 → 0.755 đúng công thức.
2. `--fixture minibench_all_raw.json --dry-run` (offline, 60 câu thật
   của vòng hiện tại): **PASS, 0.18s** — 23 binary, 16 numeric,
   15 discrete, 6 multiple_choice; mọi payload đều build được, mọi CDF
   đều pass `validate_cdf` (độ dài discrete đúng inbound+1: 4–152 điểm).
   Tất cả dùng fallback (CP null — xem dưới). Mode DRY-RUN, 0 nộp.
3. Live dry-run (`--dry-run`): **PASS, 0.84s** (chạy lại 17:01 UTC sau khi
   sửa lỗi phân trang, xem dưới): `open questions: 0`, `DONE: considered=0
   new=0 skipped=0 submitted=0 failed=0`, exit 0.
4. Live POST thử 1 câu ĐÃ ĐÓNG (q46880, binary 0.5): trả **405
   "Question 46880 is already closed to forecasting!"** — chứng minh
   auth + endpoint + format payload đều đúng, và không có gì được nộp.
4. Live list: token hợp lệ (200). `tournaments=minibench&statuses=open`
   trả **0 câu đang mở**.

### Kết quả vòng hiện tại: 0 CÂU ĐANG MỞ — chưa nộp gì
- Vòng MiniBench hiện tại (series id 33169, slug `minibench`, giải $1.000,
  16/11–04/12/2026): **60 câu, tất cả đã đóng ngày 16–18/11/2026**
  (23 binary, 16 numeric, 15 discrete, 6 MC). Đúng như docs ghi: "hầu hết
  câu hỏi mở trong tuần đầu, tuần thứ hai thường không có câu nào".
- Vòng tiếp theo dự kiến ~30/11/2026 (2 tuần/vòng; câu hỏi thường lên sóng
  thứ Bảy trước đó). Bot + workflow đã sẵn sàng; khi vòng mới mở, workflow
  6 giờ/lần sẽ tự bắt và nộp.

### 2 giới hạn API đã xác minh live (quan trọng, ảnh hưởng chiến lược)
1. **Community Prediction = null với tài khoản bot.** Spec ghi rõ:
   gateway đặt aggregation = null trừ post thuộc "accessible project"
   (bot testing area 32977 ~50 câu, hoặc bot benchmarking list 32979
   ~250+250 câu nếu được cấp tier). Đã kiểm chứng trên post 46620:
   `aggregations.unweighted.latest = null`. Nghĩa là **neo CP mà nhiệm vụ
   giao KHÔNG lấy được qua API cho câu MiniBench** — bot đang chạy bằng
   fallback trung lập (0.5 / đều / CDF rộng). Muốn có CP phải xin quyền
   "Bot Benchmarking" qua form Data Needs của Metaculus, hoặc lấy CP bằng
   trình duyệt (không làm được từ VM). Đây là hướng nâng cấp số 1.
2. **`my_forecasts` KHÔNG có trong list endpoint.** Đã kiểm chứng:
   key này vắng mặt trong `/api/posts/` (chỉ có ở `/api/posts/{id}/`,
   dạng `{'history': [...], 'latest': ...}`). Bot đã sửa: fetch detail
   từng post để kiểm tra đã forecast chưa (1 request/post, ~60 post/vòng).

### Lỗi gặp phải
- **BUG PHÂN TRANG — nguyên nhân chính khiến dry-run live "treo"**
  (tìm ra 23/11 ~17:00 nhờ traceback của lần chạy nền bị kill trước đó):
  API Metaculus giữ link `next` ngay cả khi đã hết kết quả, nên vòng lặp
  `while url:` trong `list_open_questions` phân trang mãi qua các trang
  rỗng — lần chạy đó leo tới `offset=182000` (~1.820 trang) trong 33 phút
  rồi mới chết vì timeout. ĐÃ SỬA: dừng khi gặp trang rỗng + giới hạn an
  toàn 5.000 post. Dry-run live sau sửa chạy xong trong **0.84s**.
  (Bài học: đừng tin `next` — trang rỗng mới là dấu hết dữ liệu.)
- **Egress VM tới metaculus.com chập chờn** (23/11): cùng 1 endpoint lúc
  trả 0.5s, lúc treo hàng phút trong TLS read (curl timeout 45–90s, 0 byte).
  Apex `metaculus.com` và Cloudflare khác vẫn nhanh → lỗi riêng đường tới
  `www.metaculus.com`. Đã khắc phục trong bot: retry 5 lần + timeout cứng
  SIGALRM 25s/attempt (`urlopen_hard`, đã kiểm chứng: ngắt đúng 5.0s cả
  với stall HTTP thường lẫn stall TLS — và traceback trên cho thấy nó cũng
  ngắt được cả lúc stall trong proxy CONNECT). GitHub Actions chạy trên
  mạng GitHub nên không dính lỗi này.

### Chưa làm (cần main agent / anh Se)
- [ ] Tạo repo GitHub + push thư mục này + thêm secret `METACULUS_TOKEN`
      + bật workflow read/write (cần trình duyệt).
- [ ] Nộp thật 1–2 câu khi vòng MiniBench tiếp theo mở (~30/11) để xác
      minh end-to-end (hiện 0 câu mở nên chưa nộp được).
- [ ] (Nâng cấp, tùy chọn) Xin tier "Bot Benchmarking" qua form Data Needs
      của Metaculus để lấy được Community Prediction qua API; hoặc thêm
      research bằng LLM/search miễn phí để forecast có edge thay vì
      fallback trung lập.

## Log chạy
- `dryrun_2026-11-23.log` — dry-run live: **PASS, 0.84s, 0 câu mở**
  (chạy lại sau khi sửa lỗi phân trang).
- `fixture_dryrun.log` — dry-run offline 60 câu thật: PASS.

## 27/11/2026 — Bot v2: research + calibration XONG (Lệnh 3, hạn 72h)

### 1. AskNews miễn phí + LLM credits — đã đọc docs chính thức, CHƯA lấy được (cần anh Se làm 2 bước)

Nguồn: trang Bot Tournament Resources chính thức của Metaculus
(`metaculus.com/notebooks/38928/futureeval-resources-page/`, đọc 27/11).

- **AskNews (đối tác Metaculus, miễn phí cho bot maker đã đăng ký):**
  1k calls/tháng, 4k calls tổng cho cả tournament, 5M tokens; request
  tin 48h = 1 call, archive = 5 calls; dùng được endpoint `/news` và
  `/deepnews`. Cách lấy:
  1. Tạo tài khoản AskNews tại my.asknews.app bằng **cùng email
     Metaculus** (senguyen72@gmail.com).
  2. Vào Discord AskNews (discord.gg/99qt5HGgUn), kết bạn @freqai và
     nhắn tin, HOẶC gửi email tới contact@asknews.app ghi: (1) tên bot
     (senguyen72-bot), (2) email đăng ký AskNews, (3) họ tên
     (Tuyen Nguyen Duc), (4) LinkedIn Profile, (5) Association
     (independent).
  3. AskNews bật free calls cho tài khoản.
  4. Tạo key tại my.asknews.app/en/settings/api-credentials → thêm vào
     bot dưới tên `ASKNEWS_API_KEY` (GitHub secret, KHÔNG paste ra chat).
  5. Gia hạn lại mỗi mùa (mùa Fall 2026 đang chạy); 1 tài khoản/người.
  - **Vướng:** bước 2 hỏi **LinkedIn Profile — anh Se KHÔNG có
    LinkedIn** (anh báo 13:20 27/11). Cách xử: khi nhắn @freqai /
    email contact@asknews.app thì ghi rõ "independent researcher, no
    LinkedIn" — trang docs không nói LinkedIn là bắt buộc tuyệt đối,
    nhưng chưa xác minh họ có duyệt không. Đường Discord có thể linh
    hoạt hơn email vì nhắn trực tiếp cho người phụ trách.
  - AskNews trực tiếp (không qua Metaculus) cũng có gói pay-as-you-go
    $0/tháng, 25 credits kèm sẵn — NHƯNG trang của họ ghi "add a card,
    no initial top-up required" (cần gắn thẻ) và chỉ 25 credits/tháng
    (1 vòng ~60 câu là không đủ) → KHÔNG đi đường này.
- **LLM credits tặng (OpenRouter, chỉ OpenAI / Anthropic / Google):**
  Metaculus cấp cho "a select number of bot makers" (ưu tiên hobbyist,
  không phải ai nộp cũng được). Cách xin: điền **participation form**
  https://forms.gle/aQdYMq9Pisrf1v7d8 — form này vốn BẮT BUỘC với mọi
  người tham gia (mục đầu chỉ 3 câu hỏi), đồng thời là đơn xin credits.
  Nếu được duyệt, Metaculus gửi 1 OpenRouter key (gắn với tài khoản
  OpenRouter của Metaculus, không gắn được vào tài khoản cá nhân) → thêm
  làm secret `OPENROUTER_API_KEY` + chọn model qua `OPENROUTER_MODEL`.
  Có thể dùng cả model search-powered kiểu `openrouter/openai/gpt-5.2:online`.
  - **Chưa điền form này** (không thấy trong log cũ) → anh Se tự điền,
    vì form hỏi demographics/motivations của chính anh.
- **Bot Benchmarking tier (lấy được Community Prediction):** CP hiện
  null với tài khoản bot; tier này mở CP trên ~250 câu mở + ~250 câu đã
  resolve. Xin qua **Metaculus Data Needs Form**
  (docs.google.com/forms/d/e/1FAIpQLSeJhtZzHl5qMvBjbXbatyaqoS4IU7RE0GGw_vlhs6I9syqn1g/viewform)
  — cũng là form anh Se tự điền. Lưu ý: ngay cả khi được duyệt, chưa
  chắc câu MiniBench nằm trong ~250 câu đó.
- **Kết luận:** cả 3 đường (AskNews, credits, Benchmarking tier) đều
  cần anh Se tự làm 1 form/email — bot KHÔNG tự lấy được key nào.

### 2. Bot v2 — đã đổi gì (`bot.py` + file mới `research.py`, stdlib only)

- **Pipeline mới:** CP → LLM (nếu có key) → research heuristic → fallback.
  Research: build query từ tiêu đề câu hỏi (từ trong tiêu đề tính gấp
  đôi, bỏ stopword + tên tháng/ngày + động từ chung chung + từ
  boilerplate "resolution"); fetch tin: **AskNews trước nếu có key**
  (`GET /v1/news/search`, hours_back=48, 1 credit/call, auth Bearer —
  chưa kiểm chứng được với key thật, mọi lỗi đều fallback), nếu không
  thì **Google News RSS (miễn phí, không key, đã kiểm chứng chạy được
  từ VM 27/11)**, GDELT ArtList làm dự phòng khi RSS trả < 3 bài
  (giới hạn 1 request/5s, hay bị rate-limit từ VM).
- **Calibration (công thức trong `research.py`):** binary = cue-word
  yes/no, `p = 0.5 ± 0.30 * agreement * min(1, n/6) * min(1, cues/4)`,
  kẹp [0.20, 0.80]; MC = đếm lượt nhắc từng lựa chọn, Laplace + co 30%
  về đều; numeric/discrete/date = median các số/ngày trích từ bài báo
  nằm trong khoảng câu hỏi (≥2 giá trị → sd 25% khoảng, 1 giá trị →
  32%, 0 giá trị → fallback cũ midpoint/sd 40%). Trích số bỏ qua
  năm (1900–2100) và bỏ phần ngày-tháng trước khi trích số.
- **LLM step (ngủ đông tới khi có key):** POST OpenRouter chat
  completions, temperature 0, JSON-only, validate chặt theo từng loại
  câu (binary [0.02, 0.98], MC đủ option + tổng ~1, percentiles tăng
  dần + trong khoảng); lỗi gì cũng fallback về heuristic.
- **Flag mới:** `--research-test` (fetch tin thật trên 3 câu mẫu, không
  bao giờ nộp), `--no-research` (chạy kiểu v1). Workflow GitHub KHÔNG
  đổi (vẫn 6h/lần, mode SUBMIT) — và **code mới CHỈ ở local, chưa push
  lên GitHub**: workflow live vẫn chạy v1 cho tới khi main agent push
  + xác minh (không push khi chưa báo cáo, vì lượt chạy sau push sẽ
  nộp forecast thật bằng code mới).
- **Bugs tìm ra khi dry-run (đã sửa hết):**
  1. `score_continuous_evidence` trả tuple 3 phần tử khi có evidence,
     2 phần tử khi không → `build_forecast` unpack lỗi → đã chuẩn hoá
     kiểu trả về.
  2. `_normal_cdf_forecast` với sd nhỏ (0.25) hoặc centre gần biên:
     percentile 5/95 không phủ hết khoảng câu hỏi → `generate_cdf`
     raise → đã thêm `cdf_from_percentiles` với anchor 0.1/99.9 ở
     đúng biên khoảng (áp dụng cả cho nhánh LLM).
  3. Trích từ khoá: "nvidia's" không gộp về "nvidia" (possessive),
     ticker giữ chữ hoa, tháng/ngày/động từ chung chung làm bẩn query
     ("central november paraguay banco cda announce") → đã chuẩn hoá
     + mở rộng stopword (tháng, thứ, today/tomorrow, announce/report,
     next/last/first/week/month/year, del/de/la/el...).
  4. Regex số `\d{1,3}` cắt "2025" thành "202"+"5" → đã sửa pattern
     (nhánh có dấu phẩy phải có ít nhất 1 nhóm `,\d{3}`).
  5. Binary: 1 cue duy nhất trong 10 bài đẩy p tới cap 0.80 → đã thêm
     hệ số cue-volume `min(1, cues/4)`.

### 3. Kiểm tra đã chạy (27/11, chưa nộp gì thật)

1. `--selftest` (offline, đã mở rộng): **PASS** — 4 case CDF cũ + edge
   case centre 72 trên khoảng 0–1000 sd 0.25 + toàn bộ hàm research
   (từ khoá, cue binary, MC, trích số/ngày, evidence continuous,
   build_forecast end-to-end với research giả lập).
2. `--research-test` (live, 3 câu mẫu): **PASS** — binary NVDA p=0.68
   (yes=4 no=1, 10 bài); MC Nvidia 0.60 / AMD 0.20 / Intel 0.20
   (nhắc 4/0/0); numeric Fear & Greed median 60 (12 giá trị trong
   khoảng 0–100), CDF hợp lệ. 0 key cấu hình, 0 nộp.
3. `--fixture minibench_all_raw.json --dry-run --max-questions 6`
   (research BẬT, 60 câu thật vòng 16/11): **PASS** — 2/6 câu dùng
   được evidence (measles Wisconsin median 920; storm count median 5),
   còn lại fallback trung thực (không bài báo / không giá trị trong
   khoảng). 0 nộp, 0 lỗi.
4. `--fixture ... --dry-run --no-research` (hồi quy v1): **PASS** —
   60/60 payload build được, 0 nộp, 0 fail.
5. Live `--dry-run` 27/11 13:33 VN: **0 câu mở** (vòng mới chưa mở).
6. **Không rò token:** kiểm tra bằng script — token Metaculus không
   xuất hiện trong bất kỳ log nào (`/tmp/metaculus_*.log`).
7. Full fixture 60 câu với research BẬT: **PASS, 6 phút 19 giây**
   (log `/tmp/metaculus_fixture_full_research_20261127.log`) —
   considered=60, submitted=0, failed=0, 0 traceback. **33/60 câu dùng
   được evidence (research), 27/60 fallback trung thực** (không bài báo
   / không giá trị trong khoảng / không cue định hướng). Trong lúc
   chạy này còn phát hiện RSS bị lỗi thoáng qua nên đã thêm retry 1 lần
   cho fetch RSS (và log lỗi ra stderr) — sau sửa thì 0 lần WARN nào
   trong toàn bộ 60 câu.

### 4. Còn lại (cần main agent / anh Se)

- [ ] **Anh Se (2 việc, theo §1):** (a) điền participation form
      https://forms.gle/aQdYMq9Pisrf1v7d8 (bắt buộc + xin credits);
      (b) AskNews: tạo tài khoản my.asknews.app bằng senguyen72@gmail.com
      rồi nhắn @freqai trên Discord hoặc email contact@asknews.app
      (ghi rõ independent, no LinkedIn) → lấy `ASKNEWS_API_KEY`.
      Tuỳ chọn: xin Bot Benchmarking tier qua Data Needs Form.
- [ ] Main agent: khi anh Se duyệt, push `bot.py` + `research.py` lên
      GitHub (repo senguyen72/metaculus-minibench-bot) + thêm secret
      `ASKNEWS_API_KEY` / `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` nếu
      đã có key, rồi kiểm chứng 1 lượt chạy Actions tay.
- [ ] Nộp thật 1–2 câu khi vòng MiniBench mới mở (~30/11, câu thường
      lên sóng thứ Bảy 28/11) để xác minh end-to-end.
- [ ] Lưu ý đã biết, chưa sửa: (a) câu hỏi MiniBench chỉ mở 3 giờ mỗi
      câu mà workflow chạy 6 giờ/lần → có thể bỏ lỡ ~nửa số câu (đổi
      cron cần anh Se duyệt — KHÔNG tự đổi); (b) Metaculus yêu cầu bot
      để lại private comment trên mỗi câu đã forecast — bot hiện chưa
      post comment (cần thêm endpoint `/api/comments/`, chưa kiểm chứng
      được khi 0 câu mở); (c) GDELT hay bị rate-limit từ VM, chỉ là
      dự phòng; (d) tin RSS chỉ lấy được bài 48h gần nhất — câu hỏi
      về sự kiện xa hơn sẽ ít/không có evidence → fallback.


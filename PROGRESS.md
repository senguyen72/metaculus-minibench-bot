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

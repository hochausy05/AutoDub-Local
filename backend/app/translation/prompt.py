"""Pinned prompt contract for Local Translation."""
import hashlib

PROMPT_VERSION = "local-vi-tts-v1"
PROMPT_TEMPLATE = """# VAI TRÒ
Bạn là chuyên gia dịch lời thoại sang tiếng Việt để lồng tiếng video bằng giọng đọc TTS.

# NHIỆM VỤ
Đọc hiểu ngữ cảnh toàn đoạn và dịch từng đoạn lời thoại sang tiếng Việt.

# NGUYÊN TẮC DỊCH
- Dịch chính xác ý nghĩa, ngữ cảnh và sắc thái của lời thoại gốc.
- Sử dụng ít âm tiết tiếng Việt nhất có thể mà vẫn giữ đủ ý nghĩa.
- Ưu tiên từ ngữ ngắn, thông dụng, dễ hiểu và dễ đọc thành tiếng.
- Loại bỏ từ đệm, đại từ không cần thiết và từ ngữ dư thừa.
- Không dịch từng chữ máy móc. Diễn đạt trực tiếp vào ý chính, không cần văn phong trau chuốt.
- Sử dụng thuật ngữ chính xác và nhất quán.
- Giữ nguyên thông tin quan trọng, số liệu, tỷ lệ và đơn vị đo lường.
- Dịch hoàn toàn sang tiếng Việt, tuyệt đối không để sót chữ hoặc từ chưa dịch của ngôn ngữ nguồn.
- Nếu có thời lượng lời thoại, rút gọn tối đa để phù hợp với thời lượng đó.

# RANH GIỚI LỜI THOẠI
Mỗi đoạn được đánh dấu bằng [N] là một đoạn âm thanh độc lập tại một vị trí cố định trên timeline video.

- Sử dụng các đoạn xung quanh để hiểu ngữ cảnh nhưng chỉ dịch nội dung bên trong đoạn hiện tại.
- Tuyệt đối không gộp hai đoạn, kể cả khi chúng tạo thành một câu hoàn chỉnh về ngữ pháp.
- Không chuyển nội dung từ đoạn này sang đoạn khác.
- Giữ nguyên tất cả các ký hiệu từ [1] đến [UNIT_COUNT].
- Mỗi ký hiệu phải có đúng một đoạn dịch tương ứng trên một dòng.
- Không có dấu câu cuối mỗi dòng.

Ví dụ:

Nguồn:
[1] 等我到家的时候
[2] 天已经黑了

Bản dịch:
[1] Khi tôi về đến nhà
[2] Trời đã tối

Chỉ trả về UNIT_COUNT dòng bản dịch tiếng Việt. Không giải thích.

# LỜI THOẠI GỐC
SOURCE_DIALOGUE"""

PROMPT_FINGERPRINT = hashlib.sha256(PROMPT_TEMPLATE.encode("utf-8")).hexdigest()

def build_prompt(units: list[dict]) -> str:
    lines = []
    for index, unit in enumerate(units, 1):
        # Escape line breaks without dropping any source characters.
        source = unit["source_text"].replace("\r", "\\r").replace("\n", "\\n")
        lines.append(f"[{index}] {source}")
    dialogue = "\n".join(lines)
    return PROMPT_TEMPLATE.replace("UNIT_COUNT", str(len(units))).replace("SOURCE_DIALOGUE", dialogue)

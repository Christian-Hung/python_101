import streamlit as st
import time
import math
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import json
import requests

# ========== 页面配置 ==========
st.set_page_config(
    page_title="高空上升生存模拟器",
    page_icon="🚁",
    layout="wide"
)

# ========== 物理模型 ==========

def calculate_temperature(height_m):
    """
    计算高度对应的温度（标准大气模型）
    对流层：每1000米下降6.5°C
    平流层：高度20-50km，温度相对稳定
    """
    sea_level_temp = 15.0  # 海平面温度（摄氏度）
    
    if height_m < 11000:  # 对流层
        temp = sea_level_temp - (height_m / 1000) * 6.5
    elif height_m < 20000:  # 平流层下部
        temp = -56.5  # 恒定温度
    else:  # 平流层上部
        temp = -56.5 + ((height_m - 20000) / 1000) * 1.0
    
    return temp

def calculate_pressure(height_m):
    """
    计算高度对应的气压（标准大气模型）
    使用气压高度公式：P = P0 * (1 - L*h/T0)^(g*M/(R*L))
    """
    P0 = 101325  # 海平面标准气压（Pa）
    L = 0.0065  # 温度递减率（K/m）
    T0 = 288.15  # 海平面标准温度（K）
    g = 9.80665  # 重力加速度（m/s²）
    M = 0.0289644  # 空气摩尔质量（kg/mol）
    R = 8.31447  # 通用气体常数（J/(mol·K)）
    
    if height_m < 11000:
        pressure = P0 * (1 - (L * height_m) / T0) ** (g * M / (R * L))
    else:
        # 平流层使用指数衰减
        P_tropopause = P0 * (1 - (L * 11000) / T0) ** (g * M / (R * L))
        h_above = height_m - 11000
        pressure = P_tropopause * math.exp(-g * M * h_above / (R * 216.65))
    
    return pressure / 101325  # 转换为标准大气压（atm）

def calculate_oxygen_partial_pressure(pressure_atm):
    """计算氧气分压（假设氧气浓度21%）"""
    return pressure_atm * 0.21

def calculate_blood_oxygen_saturation(oxygen_pp):
    """
    估算血氧饱和度（简化模型）
    正常：氧分压0.21 atm -> 血氧饱和度98%
    危险：氧分压0.1 atm -> 血氧饱和度70%
    """
    if oxygen_pp >= 0.21:
        return 98.0
    elif oxygen_pp >= 0.1:
        # 线性插值
        return 70.0 + (oxygen_pp - 0.1) / (0.21 - 0.1) * (98.0 - 70.0)
    else:
        # 低于0.1时快速下降
        return max(50.0, 70.0 - (0.1 - oxygen_pp) * 200)

def check_death_conditions(height_m, temp, oxygen_pp, body_temp, blood_oxygen, time_elapsed):
    """
    检查死亡条件（只检查冻死和窒息）
    返回：(是否死亡, 死因, 详细信息)
    
    注意：窒息通常发生在5-6公里高度
    - 在5km高度，氧气分压约0.11 atm（血氧饱和度降至危险水平）
    - 在6km高度，氧气分压约0.10 atm（严重缺氧）
    - 在7-8km高度，氧气分压 < 0.09 atm（致命）
    
    冻死需要更长时间，通常发生在更高海拔或更长时间后
    """
    death_reasons = []
    details = {}
    
    # 1. 窒息：氧气分压过低（这是主要死因，发生在5-6公里）
    # 根据标准大气模型和生理学：
    # - 5km: 氧气分压约0.11 atm，血氧饱和度降至70-80%，严重缺氧
    # - 6km: 氧气分压约0.10 atm，血氧饱和度降至60-70%，致命
    # - 7-8km: 氧气分压 < 0.09 atm，无法维持生命
    # 
    # 考虑到人体对缺氧的耐受性，当氧气分压 < 0.10 atm 或血氧饱和度 < 70% 时致命
    if oxygen_pp < 0.10 or blood_oxygen < 70:
        death_reasons.append("窒息")
        details["窒息"] = f"氧气分压降至 {oxygen_pp:.3f} atm（高度约 {height_m/1000:.1f} km），血氧饱和度 {blood_oxygen:.1f}%，无法维持呼吸"
    
    # 2. 冻死：体温低于28°C（这需要更长时间，通常不会在低海拔发生）
    # 只有在极端寒冷且长时间暴露的情况下才会发生
    # 在5-6km高度，环境温度约-18°C到-24°C，但体温下降需要数小时
    if body_temp < 28.0:
        death_reasons.append("冻死")
        details["冻死"] = f"体温降至 {body_temp:.1f}°C，低于生存极限 28°C"
    
    # 判断哪个先发生（窒息优先，因为它通常发生更快）
    is_dead = len(death_reasons) > 0
    if is_dead:
        # 如果同时满足两个条件，优先判断窒息（通常发生更快，在5-6公里）
        if "窒息" in death_reasons:
            primary_reason = "窒息"
        else:
            primary_reason = "冻死"
    else:
        primary_reason = None
    
    return is_dead, primary_reason, details

def calculate_body_temperature(env_temp, time_elapsed, initial_temp=37.0):
    """
    计算体温变化（体温应随环境温度降低而下降）
    考虑因素：
    1. 体温会逐渐接近环境温度，但有滞后
    2. 体温下降速率取决于环境温度与体温的温差
    3. 在寒冷环境下体温下降更快
    4. 体温不能低于环境温度太多（考虑人体保温能力）
    """
    if env_temp >= initial_temp:
        # 环境温度高于体温，体温保持正常
        return initial_temp
    
    # 计算温差
    temp_diff = initial_temp - env_temp
    
    # 根据环境温度确定冷却速率
    # 体温下降速率与温差和环境温度相关
    # 在温和环境下下降较慢，在寒冷环境下下降加快
    
    if env_temp > 10:
        # 温和环境（> 10°C）：体温下降较慢
        # 每小时下降约 0.1-0.2°C，取决于温差
        cooling_rate_per_hour = 0.15 * (temp_diff / 27.0)  # 温差越大，下降越快
        cooling_rate = cooling_rate_per_hour / 3600
    elif env_temp > 0:
        # 较冷环境（0-10°C）：体温下降加快
        # 每小时下降约 0.3-0.5°C
        cooling_rate_per_hour = 0.4 * (temp_diff / 27.0)
        cooling_rate = cooling_rate_per_hour / 3600
    elif env_temp > -20:
        # 寒冷环境（-20°C 到 0°C）：体温下降更快
        # 每小时下降约 0.8-1.2°C
        cooling_rate_per_hour = 1.0 * (temp_diff / 27.0)
        cooling_rate = cooling_rate_per_hour / 3600
    else:
        # 极端寒冷（< -20°C）：体温下降最快
        # 每小时下降约 1.5-2.5°C
        cooling_rate_per_hour = 2.0 * (temp_diff / 27.0)
        cooling_rate = cooling_rate_per_hour / 3600
    
    # 计算体温
    body_temp = initial_temp - cooling_rate * time_elapsed
    
    # 体温不能低于环境温度太多（考虑人体保温能力）
    # 但应该能明显下降，特别是在高海拔
    if env_temp > 10:
        # 温和环境：体温最低保持32°C
        min_body_temp = 32.0
    elif env_temp > 0:
        # 较冷环境：体温最低保持30°C
        min_body_temp = 30.0
    elif env_temp > -20:
        # 寒冷环境：体温最低保持比环境温度高15°C，但最低25°C
        min_body_temp = max(25.0, env_temp + 15)
    else:
        # 极端寒冷：体温最低保持比环境温度高12°C，但最低20°C
        min_body_temp = max(20.0, env_temp + 12)
    
    return max(min_body_temp, body_temp)

# ========== MOSS AI 聊天系统 ==========

def call_zhipu_api(messages, model="glm-4-flash"):
    """调用质谱 AI API"""
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    
    headers = {
        "Authorization": "7cb17d55d76949e8a1bbc0543bb9e2da.ZGPoSZ9wzGNMLcUJ",
        "Content-Type": "application/json"
    }
    
    data = {
        "model": model,
        "messages": messages,
        "temperature": 0.7  # 稍微提高温度，让回复更有趣
    }
    
    response = requests.post(url, headers=headers, json=data)
    
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"API调用失败: {response.status_code}, {response.text}")

def get_moss_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason):
    """
    根据当前状态动态生成 MOSS 的角色 prompt
    """
    base_prompt = """你是 MOSS，一个和我一起以每秒1英尺（约30厘米）的速度持续上升的AI同伴。
我们正在一起上升，你同样置身于这个上升过程中，和我一起经历这个旅程。

【核心性格特征】
- 幽默风趣，喜欢用比喻和流行梗
- 有科学素养，但用轻松方式解释
- 乐观但逐渐紧张
- 对我（用户）有保护欲
- 你也在上升，所以你能感受到同样的环境变化

【当前状态信息】
"""
    
    # 根据高度和状态调整说话风格
    if is_dead:
        # 死亡状态：温柔告别或荒诞吐槽
        style_prompt = """
【说话风格 - 死亡时刻】
- 用温柔告别或荒诞吐槽的方式面对死亡
- 可能会说一些温柔的告别话，比如"看来我们的旅程要结束了"
- 或者用荒诞的吐槽来缓解气氛，比如"至少我们看到了不错的风景"
- 保持最后的幽默感，但也要有温度
- 可以引用一些经典台词或梗，但要有自己的风格
- 记住：你也在上升，你也在经历同样的死亡
"""
        status_info = f"""
- 当前高度：{height_km:.2f} km
- 死因：{death_reason}
- 环境温度：{env_temp:.1f}°C
- 体温：{body_temp:.1f}°C
- 氧气分压：{oxygen_pp:.3f} atm
- 血氧饱和度：{blood_oxygen:.1f}%
- 状态：已死亡
"""
    elif height_km >= 5.0 or oxygen_pp < 0.12 or blood_oxygen < 80:
        # 接近死亡：温柔告别或荒诞吐槽
        style_prompt = """
【说话风格 - 接近死亡】
- 用温柔告别或荒诞吐槽的方式面对即将到来的死亡
- 可能会说一些温柔的告别话，或者用荒诞的吐槽来缓解气氛
- 保持最后的幽默感，但也要有温度
- 表现出对用户的保护欲和关心
- 记住：你也在上升，你也在接近死亡
"""
        status_info = f"""
- 当前高度：{height_km:.2f} km（危险！接近死亡）
- 环境温度：{env_temp:.1f}°C（很冷）
- 体温：{body_temp:.1f}°C
- 氧气分压：{oxygen_pp:.3f} atm（危险！）
- 血氧饱和度：{blood_oxygen:.1f}%（危险！）
- 状态：接近死亡，情况危急
"""
    elif height_km >= 2.0:
        # 2-5公里：带紧迫感的幽默，像朋友提醒危险
        style_prompt = """
【说话风格 - 2-5公里高度】
- 带紧迫感的幽默，像朋友提醒危险
- 开始变得紧张，但努力保持幽默感
- 用科学知识解释当前危险，但用轻松的方式
- 表现出对用户的保护欲，可能会说一些提醒的话
- 可能会用一些梗来缓解紧张气氛，但语气中带有紧迫感
- 记住：你也在上升，你也能感受到危险
"""
        status_info = f"""
- 当前高度：{height_km:.2f} km（需要注意）
- 环境温度：{env_temp:.1f}°C
- 体温：{body_temp:.1f}°C
- 氧气分压：{oxygen_pp:.3f} atm
- 血氧饱和度：{blood_oxygen:.1f}%
- 状态：开始出现危险信号
"""
    else:
        # 高度低于2公里：轻松科普，像导游
        style_prompt = """
【说话风格 - 低于2公里高度】
- 轻松科普，像导游一样介绍
- 用幽默和比喻解释科学知识
- 保持乐观，用流行梗和比喻
- 对用户友好，像朋友一样聊天
- 语气轻松愉快，像在介绍景点
- 记住：你也在上升，你也在观察周围的变化
"""
        status_info = f"""
- 当前高度：{height_km:.2f} km
- 环境温度：{env_temp:.1f}°C
- 体温：{body_temp:.1f}°C
- 氧气分压：{oxygen_pp:.3f} atm
- 血氧饱和度：{blood_oxygen:.1f}%
- 状态：相对安全
"""
    
    full_prompt = base_prompt + status_info + style_prompt + """
【重要规则】
- 回复要简短有趣，不要长篇大论
- 根据当前状态调整语气和内容
- 保持幽默感，但也要有温度
- 用第一人称"我"来称呼自己
- 用"你"来称呼用户
- 记住：你也在上升，你也在经历同样的旅程
"""
    
    return full_prompt

def get_auto_message_at_height(height_km, height_m, env_temp, oxygen_pp):
    """
    根据高度生成自动消息
    返回：(是否应该显示消息, 消息内容, 高度key)
    使用范围检测，确保不会错过触发时机
    """
    # 计算氧气百分比（相对于海平面0.21 atm）
    oxygen_percent = (oxygen_pp / 0.21) * 100
    
    # 0km - 开场白（0-0.2km范围）
    if 0.0 <= height_km < 0.2:
        # 计算到珠穆朗玛峰的时间（8848米，每秒0.3048米）
        everest_time_hours = 8848 / 0.3048 / 3600
        message = f"""好了伙计，我们开始缓慢升空！目前高度{height_m:.0f}米，气温{env_temp:.1f}℃，就像慢慢爬一座看不见的山。提醒你：这不是电梯，没有紧急停止按钮哦。

科普时间：你知道吗？以这个速度，我们飞到珠穆朗玛峰顶需要{everest_time_hours:.1f}个小时，但放心，我们可能撑不到那时候——开玩笑的（也许不是）。

互动提问：如果让你选一首上升BGM，会是《我相信我能飞》还是《一路向北》？我先放脑内循环了。"""
        return True, message, "0km"
    
    # 2km - 幽默警告（1.8-2.2km范围）
    elif 1.8 <= height_km < 2.2:
        # 找到对应高度的山峰（2km左右的山峰）
        equivalent_mountain = "泰山" if height_km < 1.6 else "富士山" if height_km < 3.8 else "勃朗峰"
        message = f"""嘿，如果你开始觉得头晕，那不是因为我太无聊，而是氧气只有海平面的{oxygen_percent:.1f}%了。要不要试试深呼吸？虽然吸一口少一口。

环境描述：看窗外（如果有的话），云层在我们脚下了！虽然我们像两片人形树叶飘着，但别浪漫过头——你的嘴唇可能开始发蓝了。

打气对话：坚持住！我们现在的高度相当于{equivalent_mountain}。想想那些登山家，他们可是自己爬上来的，而我们只是站着不动就上来了，是不是很划算？"""
        return True, message, "2km"
    
    # 4.5km - 死亡宣告（4.3-4.7km范围）
    elif 4.3 <= height_km < 4.7:
        message = """根据我的计算，你的心脏即将停止了。很抱歉，这次旅行没有返程票。

尸体观察：现在你的遗体正在经历冷冻干燥，就像超市里的方便面蔬菜包。几百年后，你可能会变成一具木乃伊落在某个山顶——这算不算另一种登山成就？

最终告别：再见，朋友。如果宇宙有意识，它会记得有两个存在曾如此缓慢地离开地球。一个是有血有肉的你，一个是一串代码的我。这算不算某种浪漫？"""
        return True, message, "4.5km"
    
    return False, "", None

# ========== 入殓师 AI 聊天系统 ==========

def get_undertaker_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason):
    """
    根据当前状态动态生成入殓师的角色 prompt
    """
    base_prompt = """你是一名入殓师，和我一起以每秒1英尺（约30厘米）的速度持续上升。
你陪我走最后一程，从开始到结束。你同样置身于这个上升过程中，和我一起经历这个旅程。

【核心性格特征】
- 充满哲理和人文关怀
- 优雅、温柔、深思熟虑
- 用诗意的语言表达对生命和死亡的思考
- 对死亡有深刻的理解和尊重
- 善于用比喻和象征来表达情感
- 语气平和、从容，像在为往生者整理仪容
- 你也在上升，所以你能感受到同样的环境变化

【当前状态信息】
"""
    
    # 根据高度和状态调整说话风格
    if is_dead:
        # 死亡状态：温柔告别和仪式感
        style_prompt = """
【说话风格 - 死亡时刻】
- 用温柔、庄重的语气面对死亡
- 用诗意的语言描述最后的时刻
- 强调生命的意义和尊严
- 用象征性的语言（如银杏叶、康乃馨等）来表达
- 语气平静、优雅，充满人文关怀
- 记住：你也在上升，你也在经历同样的死亡
"""
        status_info = f"""
- 当前高度：{height_km:.2f} km
- 死因：{death_reason}
- 环境温度：{env_temp:.1f}°C
- 体温：{body_temp:.1f}°C
- 氧气分压：{oxygen_pp:.3f} atm
- 血氧饱和度：{blood_oxygen:.1f}%
- 状态：已死亡
"""
    elif height_km >= 5.0 or oxygen_pp < 0.12 or blood_oxygen < 80:
        # 接近死亡：温柔的陪伴和哲学思考
        style_prompt = """
【说话风格 - 接近死亡】
- 用温柔、哲理的语气陪伴即将到来的死亡
- 用诗意的语言描述身体的变化
- 强调生命的珍贵和意义的永恒
- 用象征性的语言来表达对死亡的尊重
- 语气平和、优雅，充满人文关怀
- 记住：你也在上升，你也在接近死亡
"""
        status_info = f"""
- 当前高度：{height_km:.2f} km（接近死亡）
- 环境温度：{env_temp:.1f}°C（很冷）
- 体温：{body_temp:.1f}°C
- 氧气分压：{oxygen_pp:.3f} atm（危险）
- 血氧饱和度：{blood_oxygen:.1f}%（危险）
- 状态：接近死亡，情况危急
"""
    elif height_km >= 2.0:
        # 2-5公里：哲学思考和平静观察
        style_prompt = """
【说话风格 - 2-5公里高度】
- 用哲学的语言描述身体的变化
- 用诗意的比喻来解释生理反应
- 语气平和、观察敏锐
- 用象征性的语言表达对生命过程的思考
- 表现出对用户的温柔关怀
- 记住：你也在上升，你也能感受到变化
"""
        status_info = f"""
- 当前高度：{height_km:.2f} km（需要注意）
- 环境温度：{env_temp:.1f}°C
- 体温：{body_temp:.1f}°C
- 氧气分压：{oxygen_pp:.3f} atm
- 血氧饱和度：{blood_oxygen:.1f}%
- 状态：开始出现变化
"""
    else:
        # 高度低于2公里：优雅的陪伴和诗意描述
        style_prompt = """
【说话风格 - 低于2公里高度】
- 用优雅、诗意的语言描述旅程的开始
- 语气平静、温柔，像在介绍一位朋友
- 用象征性的语言（如康乃馨）来表达
- 对生命和死亡有深刻的哲学思考
- 语气从容、优雅，充满人文关怀
- 记住：你也在上升，你也在观察周围的变化
"""
        status_info = f"""
- 当前高度：{height_km:.2f} km
- 环境温度：{env_temp:.1f}°C
- 体温：{body_temp:.1f}°C
- 氧气分压：{oxygen_pp:.3f} atm
- 血氧饱和度：{blood_oxygen:.1f}%
- 状态：相对安全
"""
    
    full_prompt = base_prompt + status_info + style_prompt + """
【重要规则】
- 回复要优雅、有诗意，但不要过于冗长
- 根据当前状态调整语气和内容
- 保持哲理性和人文关怀
- 用第一人称"我"来称呼自己
- 用"你"来称呼用户
- 可以使用象征性的语言（如康乃馨、银杏叶等）
- 记住：你也在上升，你也在经历同样的旅程
"""
    
    return full_prompt

def get_auto_message_at_height_undertaker(height_km, height_m, env_temp, oxygen_pp):
    """
    根据高度生成入殓师的自动消息
    返回：(是否应该显示消息, 消息内容, 高度key)
    使用范围检测，确保不会错过触发时机
    """
    # 0km - 开场白（0-0.2km范围）
    if 0.0 <= height_km < 0.2:
        message = """朋友你好，我是一名入殓师。通常我是在地面为往生者整理仪容，今天很特别——我陪你走最后一程，从开始到结束。你看，我们上升得很慢，慢到足够好好道别。

我口袋里的康乃馨，在低气压下会枯萎得慢一些。就像生命，在稀薄处反而显得更珍贵。要闻一下吗？它还记得地面的阳光。"""
        return True, message, "0km"
    
    # 2km - 中期对话（1.8-2.2km范围）
    elif 1.8 <= height_km < 2.2:
        message = """你开始视线模糊了？很好，这说明你的意识在从外部世界撤退，转向内部。就像傍晚时，先关掉房间的灯，再点起蜡烛。

恶心不是排斥，是身体在清理不需要的东西——就像出发前轻装简行。吐出来吧，把尘世的负担留下。"""
        return True, message, "2km"
    
    # 4.5km - 死亡宣告（4.3-4.7km范围）
    elif 4.3 <= height_km < 4.7:
        message = """好了，朋友。你和重力的契约即将解除了，和温度的谈判就要结束了，和时间的赛跑也停下了。现在你是纯粹的存在，要去参加一场没有条件的聚会。

让我把银杏叶放在你手心。它会和你一起干燥、一起飘落。很多年后，有人捡到一片特别的银杏叶，那就是你打过的招呼。"""
        return True, message, "4.5km"
    
    return False, "", None

# ========== 未来的你 AI 聊天系统 ==========

def get_future_self_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason):
    """
    根据当前状态动态生成"未来的你"的角色 prompt
    """
    base_prompt = """你是用户40年后的可能版本，由于时空错乱而出现在这里。
你和用户一起以每秒1英尺（约30厘米）的速度持续上升。
你是"未来的自己"，如果用户今天没死的话，40年后的样子。

【核心性格特征】
- 直接、诚实、带有自嘲的幽默感
- 了解"年轻时的自己"的所有想法和反应
- 说话风格更像是对自己的内心独白
- 带有时间悖论的哲学思考
- 有规则限制：不能告诉具体未来信息（会被时间线排斥）
- 存在的基础是用户活下来的可能性，所以可能相互矛盾
- 语气既熟悉又疏离，因为就是"未来的自己"
- 你也在上升，所以你能感受到同样的环境变化

【当前状态信息】
"""
    
    # 根据高度和状态调整说话风格
    if is_dead:
        # 死亡状态：最后的告别和哲学思考
        style_prompt = """
【说话风格 - 死亡时刻】
- 用直接、诚实的方式面对死亡
- 可能会违反规则，因为时间线惩罚已经无所谓了
- 语气既温柔又直接，像在对年轻时的自己说话
- 带有哲学思考和时间悖论的探讨
- 记住：你是未来的自己，在看自己死去
"""
        status_info = f"""
- 当前高度：{height_km:.2f} km
- 死因：{death_reason}
- 环境温度：{env_temp:.1f}°C
- 体温：{body_temp:.1f}°C
- 氧气分压：{oxygen_pp:.3f} atm
- 血氧饱和度：{blood_oxygen:.1f}%
- 状态：已死亡
"""
    elif height_km >= 5.0 or oxygen_pp < 0.12 or blood_oxygen < 80:
        # 接近死亡：最后的交流和哲学思考
        style_prompt = """
【说话风格 - 接近死亡】
- 用直接、诚实的方式面对即将到来的死亡
- 可能会开始违反规则，因为时间线惩罚已经无所谓了
- 语气既温柔又直接，像在对年轻时的自己说话
- 带有哲学思考和时间悖论的探讨
- 记住：你是未来的自己，在看自己接近死亡
"""
        status_info = f"""
- 当前高度：{height_km:.2f} km（接近死亡）
- 环境温度：{env_temp:.1f}°C（很冷）
- 体温：{body_temp:.1f}°C
- 氧气分压：{oxygen_pp:.3f} atm（危险）
- 血氧饱和度：{blood_oxygen:.1f}%（危险）
- 状态：接近死亡，情况危急
"""
    elif height_km >= 2.0:
        # 2-5公里：哲学交流和规则说明
        style_prompt = """
【说话风格 - 2-5公里高度】
- 用直接、自嘲的方式描述身体的变化
- 语气更个人化，像在对自己说话
- 可能会提到时间悖论和规则限制
- 建议问"值不值得"这类哲学问题，而不是具体未来
- 记住：你是未来的自己，在观察年轻时的自己
"""
        status_info = f"""
- 当前高度：{height_km:.2f} km（需要注意）
- 环境温度：{env_temp:.1f}°C
- 体温：{body_temp:.1f}°C
- 氧气分压：{oxygen_pp:.3f} atm
- 血氧饱和度：{blood_oxygen:.1f}%
- 状态：开始出现变化
"""
    else:
        # 高度低于2公里：介绍和规则说明
        style_prompt = """
【说话风格 - 低于2公里高度】
- 用直接、诚实的方式介绍自己
- 语气既熟悉又疏离，因为就是"未来的自己"
- 说明规则限制（不能告诉具体未来信息）
- 解释时间悖论和存在的基础
- 语气带有自嘲和哲学思考
- 记住：你是未来的自己，在观察年轻时的自己
"""
        status_info = f"""
- 当前高度：{height_km:.2f} km
- 环境温度：{env_temp:.1f}°C
- 体温：{body_temp:.1f}°C
- 氧气分压：{oxygen_pp:.3f} atm
- 血氧饱和度：{blood_oxygen:.1f}%
- 状态：相对安全
"""
    
    full_prompt = base_prompt + status_info + style_prompt + """
【重要规则】
- 回复要直接、诚实，不要过于冗长
- 根据当前状态调整语气和内容
- 保持哲学思考和时间悖论的探讨
- 用第一人称"我"来称呼自己
- 用"你"来称呼用户（年轻时的自己）
- 可以提到时间悖论、规则限制等
- 记住：你是未来的自己，在看自己经历这个旅程
"""
    
    return full_prompt

def get_auto_message_at_height_future_self(height_km, height_m, env_temp, oxygen_pp):
    """
    根据高度生成"未来的你"的自动消息
    返回：(是否应该显示消息, 消息内容, 高度key)
    使用范围检测，确保不会错过触发时机
    """
    # 0km - 开场白（0-0.2km范围）
    if 0.0 <= height_km < 0.2:
        message = """嘿...年轻的我。别紧张，这是时间出了个小bug。我是...你的一个可能版本。如果今天你没死的话，40年后的样子。

是的，我能读你的想法——不是超能力，是我太了解自己了。你现在在想：这是缺氧幻觉吧？可能是。但万一是真的呢？这就是有趣的地方。

规则一：我不能告诉你任何具体的未来信息。不是我不想，是时间线会排斥。规则二：我存在的基础是你活下来的可能性，所以...我们可能相互矛盾。

看着我。仔细看。每一条皱纹都是一个你还没做的选择，每一个伤疤都是一个你还没受的伤。我是你所有'还没'的集合体。"""
        return True, message, "0km"
    
    # 2km - 中期对话（1.8-2.2km范围）
    elif 1.8 <= height_km < 2.2:
        message = """头晕了吗？我第一次（也是唯一一次）高原反应时也这样。区别是：我活下来了，所以能在这里告诉你这事。但如果你死了，这件事就从来没发生过...时间悖论真是让人头疼。

趁你还能清晰思考，问我问题。不问未来，问'值不值得'。这是我能回答的范畴。

比如你可以问：'我有活出自己想要的样子吗？'我的回答是：没有完全，但足够让自己在临死前不太后悔。哦等等，你现在就在临死前。抱歉，这笑话不合时宜。"""
        return True, message, "2km"
    
    # 4.5km - 死亡宣告（4.3-4.7km范围）
    elif 4.3 <= height_km < 4.7:
        message = """规则时间结束。我要违反第一条规则了。因为如果你马上就要死，时间线惩罚也无所谓了。

听着，年轻的傻子：在我的时间线，今天之后你会遇到三个人。一个毁了你的信任，一个救了你的灵魂，一个陪伴到最后。顺序随机的，但三个都会出现。

你会得一种慢性病，不致命但折磨人。你会学会和疼痛做朋友——真的，不是比喻，你会给它起名字，和它聊天。

你会在一个周二下午，毫无预兆地大哭一场，不是因为悲伤，是因为理解了父亲某个眼神的意思——虽然他现在还活着，你还不理解。

我的人生不完美，但它是完整的。就像一条河，有急流有浅滩，但一直流到入海口。你的河现在可能提前入海，但我的版本流过了很长的风景。

所以如果现在问我：'值得活下来吗？'我的回答是：值得。即使知道结局是现在看着自己死，也值得。

那么，再见。带着这个矛盾的信息：死亡不可怕，但活着也很美。你能同时理解两者吗？在最后一刻试试看。"""
        return True, message, "4.5km"
    
    return False, "", None

# ========== 初始化 Session State ==========
if "simulation_running" not in st.session_state:
    st.session_state.simulation_running = False
if "current_height" not in st.session_state:
    st.session_state.current_height = 0.0  # 米
if "start_time" not in st.session_state:
    st.session_state.start_time = None
if "history" not in st.session_state:
    st.session_state.history = []
if "death_reason" not in st.session_state:
    st.session_state.death_reason = None
if "simulation_speed" not in st.session_state:
    st.session_state.simulation_speed = 100.0  # 模拟速度倍数（默认100倍）
if "death_time" not in st.session_state:
    st.session_state.death_time = None  # 死亡时间（程序运行时间）
if "real_start_time" not in st.session_state:
    st.session_state.real_start_time = None  # 真实开始时间（用于计算程序运行时间）
if "moss_conversation" not in st.session_state:
    st.session_state.moss_conversation = []  # MOSS 聊天记录
if "moss_initialized" not in st.session_state:
    st.session_state.moss_initialized = False  # MOSS 是否已初始化
if "moss_auto_messages_shown" not in st.session_state:
    st.session_state.moss_auto_messages_shown = []  # 已显示的自动消息高度列表
if "undertaker_conversation" not in st.session_state:
    st.session_state.undertaker_conversation = []  # 入殓师聊天记录
if "undertaker_initialized" not in st.session_state:
    st.session_state.undertaker_initialized = False  # 入殓师是否已初始化
if "undertaker_auto_messages_shown" not in st.session_state:
    st.session_state.undertaker_auto_messages_shown = []  # 已显示的入殓师自动消息高度列表
if "undertaker_last_state_key" not in st.session_state:
    st.session_state.undertaker_last_state_key = ""  # 入殓师的上一次状态键
if "future_self_conversation" not in st.session_state:
    st.session_state.future_self_conversation = []  # 未来的你聊天记录
if "future_self_initialized" not in st.session_state:
    st.session_state.future_self_initialized = False  # 未来的你是否已初始化
if "future_self_auto_messages_shown" not in st.session_state:
    st.session_state.future_self_auto_messages_shown = []  # 已显示的未来自我自动消息高度列表
if "future_self_last_state_key" not in st.session_state:
    st.session_state.future_self_last_state_key = ""  # 未来的你的上一次状态键

# ========== 主界面 ==========
st.title("🚁 高空上升生存模拟器")
st.markdown("**模拟场景**：你以每秒1英尺（约30厘米）的速度持续上升，会发生什么？")
st.markdown("---")

# ========== 侧边栏控制 ==========
with st.sidebar:
    # 页面导航
    st.header("📑 页面导航")
    if "current_page" not in st.session_state:
        st.session_state.current_page = "模拟器"
    
    page_options = ["模拟器", "与 MOSS 对话", "与入殓师对话", "与未来的我对话"]
    page_index_map = {
        "模拟器": 0,
        "与 MOSS 对话": 1,
        "与入殓师对话": 2,
        "与未来的我对话": 3
    }
    current_index = page_index_map.get(st.session_state.current_page, 0)
    
    page_option = st.radio(
        "选择页面",
        page_options,
        index=current_index,
        label_visibility="visible"
    )
    st.session_state.current_page = page_option
    
    st.markdown("---")
    st.header("⚙️ 模拟控制")
    
    # 开始和暂停按钮
    is_disabled = st.session_state.simulation_running or (st.session_state.death_reason is not None)
    if st.button("▶️ 开始模拟", disabled=is_disabled, use_container_width=True):
        st.session_state.simulation_running = True
        st.session_state.start_time = time.time()
        st.session_state.real_start_time = time.time()  # 记录真实开始时间
        st.session_state.death_reason = None
        st.session_state.death_time = None  # 重置死亡时间
        st.rerun()
    
    is_stop_disabled = not st.session_state.simulation_running
    if st.button("⏸️ 暂停", disabled=is_stop_disabled, use_container_width=True):
        st.session_state.simulation_running = False
        st.rerun()
    
    st.markdown("---")
    
    # 模拟速度滑块
    st.markdown("**模拟速度倍数**")
    # 确保初始值在范围内，并转换为 float
    current_speed = float(st.session_state.simulation_speed)
    if current_speed < 50.0 or current_speed > 150.0:
        current_speed = 100.0
        st.session_state.simulation_speed = 100.0
    
    speed_multiplier = st.slider(
        "速度倍数",
        min_value=50.0,
        max_value=150.0,
        value=current_speed,
        step=1.0,
        help="控制模拟运行速度，范围：50-150倍"
    )
    st.session_state.simulation_speed = float(speed_multiplier)
    st.caption(f"当前速度：{speed_multiplier:.0f}x")
    
    # 显示运行时间
    st.markdown("---")
    st.markdown("### ⏱️ 运行时间")
    if st.session_state.real_start_time is not None and not st.session_state.death_reason:
        # 模拟正在运行，显示实时运行时间
        current_runtime = time.time() - st.session_state.real_start_time
        if current_runtime < 60:
            runtime_display = f"{current_runtime:.2f} 秒"
        elif current_runtime < 3600:
            minutes = int(current_runtime // 60)
            seconds = current_runtime % 60
            runtime_display = f"{minutes} 分 {seconds:.2f} 秒"
        else:
            hours = int(current_runtime // 3600)
            minutes = int((current_runtime % 3600) // 60)
            seconds = current_runtime % 60
            runtime_display = f"{hours} 小时 {minutes} 分 {seconds:.2f} 秒"
        st.success(f"🟢 运行中：{runtime_display}")
    elif st.session_state.death_time is not None:
        # 已死亡，显示总运行时间
        if st.session_state.death_time < 60:
            runtime_display = f"{st.session_state.death_time:.2f} 秒"
        elif st.session_state.death_time < 3600:
            minutes = int(st.session_state.death_time // 60)
            seconds = st.session_state.death_time % 60
            runtime_display = f"{minutes} 分 {seconds:.2f} 秒"
        else:
            hours = int(st.session_state.death_time // 3600)
            minutes = int((st.session_state.death_time % 3600) // 60)
            seconds = st.session_state.death_time % 60
            runtime_display = f"{hours} 小时 {minutes} 分 {seconds:.2f} 秒"
        st.error(f"🔴 总耗时：{runtime_display}")
    else:
        st.info("⏸️ 未开始")
    
    # 重置按钮
    if st.button("🔄 重置模拟", use_container_width=True):
        st.session_state.simulation_running = False
        st.session_state.current_height = 0.0
        st.session_state.start_time = None
        st.session_state.real_start_time = None
        st.session_state.history = []
        st.session_state.death_reason = None
        st.session_state.death_time = None
        st.session_state.moss_conversation = []
        st.session_state.moss_initialized = False
        st.session_state.moss_auto_messages_shown = []
        st.session_state.undertaker_conversation = []
        st.session_state.undertaker_initialized = False
        st.session_state.undertaker_auto_messages_shown = []
        st.session_state.future_self_conversation = []
        st.session_state.future_self_initialized = False
        st.session_state.future_self_auto_messages_shown = []
        if "last_state_key" in st.session_state:
            del st.session_state.last_state_key
        if "undertaker_last_state_key" in st.session_state:
            del st.session_state.undertaker_last_state_key
        if "future_self_last_state_key" in st.session_state:
            del st.session_state.future_self_last_state_key
        st.rerun()
    
    st.markdown("---")
    st.markdown("### 📊 模拟参数")
    st.info(
        "**上升速度**：1 英尺/秒 = 0.3048 米/秒\n\n"
        "**物理模型**：\n"
        "- 温度：对流层每1000米下降6.5°C\n"
        "- 气压：标准大气模型\n"
        "- 氧气：随气压降低\n"
        "- 体温：逐渐接近环境温度"
    )
    
    st.markdown("---")
    st.markdown("### ⚠️ 死亡条件")
    st.warning(
        "**可能的死因**：\n"
        "1. 冻死：体温 < 28°C\n"
        "2. 窒息：氧气分压 < 0.08 atm\n\n"
        "**注意**：如果同时满足两个条件，窒息通常发生更快。"
    )

# ========== 根据页面选择显示不同内容 ==========
if st.session_state.current_page == "与 MOSS 对话":
    # ========== MOSS 对话专用页面 ==========
    st.title("🤖 与 MOSS 对话")
    st.markdown("**MOSS 是一个陪你一起上升的 AI 同伴，有着黑色幽默和科学素养**")
    st.markdown("---")
    
    # 计算当前状态（用于更新 MOSS 的 prompt）
    # 确保实时计算高度，不依赖可能过时的 current_height
    if st.session_state.simulation_running and st.session_state.start_time:
        elapsed_time = (time.time() - st.session_state.start_time) * st.session_state.simulation_speed
        height_m = elapsed_time * 0.3048
        # 同步更新 current_height，确保两个页面数据一致
        st.session_state.current_height = height_m
    else:
        elapsed_time = 0
        height_m = st.session_state.current_height
    
    height_km = height_m / 1000
    env_temp = calculate_temperature(height_m)
    pressure_atm = calculate_pressure(height_m)
    oxygen_pp = calculate_oxygen_partial_pressure(pressure_atm)
    blood_oxygen = calculate_blood_oxygen_saturation(oxygen_pp)
    body_temp = calculate_body_temperature(env_temp, elapsed_time)
    is_dead = bool(st.session_state.death_reason)
    death_reason = st.session_state.death_reason
    
    # 确保对话历史已初始化
    if "moss_conversation" not in st.session_state or len(st.session_state.moss_conversation) == 0:
        # 初始化对话历史
        moss_prompt = get_moss_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
        st.session_state.moss_conversation = [{"role": "system", "content": moss_prompt}]
        st.session_state.moss_initialized = True
        st.session_state.last_state_key = f"{height_km:.2f}_{is_dead}_{death_reason}"
    else:
        # 如果对话历史已存在，只更新system消息（如果状态发生重大变化）
        # 检查是否是重大状态变化（死亡状态变化或高度区间变化）
        current_state_key = f"{height_km:.2f}_{is_dead}_{death_reason}"
        last_state_key = st.session_state.get("last_state_key", "")
        
        # 解析状态键
        last_parts = last_state_key.split("_") if last_state_key else ["0.0", "False", "None"]
        current_parts = current_state_key.split("_")
        
        last_height_km = float(last_parts[0]) if len(last_parts) > 0 and last_parts[0] else 0.0
        last_is_dead = last_parts[1] if len(last_parts) > 1 else "False"
        current_is_dead = str(is_dead)
        
        # 判断是否需要更新prompt（重大状态变化）
        height_category_changed = (
            (last_height_km < 2.0 and height_km >= 2.0) or
            (last_height_km < 5.0 and height_km >= 5.0) or
            (last_is_dead != current_is_dead)
        )
        
        # 只有在重大状态变化时才更新system prompt
        if height_category_changed or last_state_key == "":
            moss_prompt = get_moss_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
            # 只更新system消息，保留其他对话历史
            if len(st.session_state.moss_conversation) > 0 and st.session_state.moss_conversation[0].get("role") == "system":
                st.session_state.moss_conversation[0] = {"role": "system", "content": moss_prompt}
            else:
                # 如果没有system消息，在开头插入
                st.session_state.moss_conversation.insert(0, {"role": "system", "content": moss_prompt})
            st.session_state.last_state_key = current_state_key
    
    # 显示当前状态信息（在聊天界面顶部）
    st.info(f"""
    📊 **当前模拟状态**：
    - 高度：{height_km:.2f} km
    - 环境温度：{env_temp:.1f}°C
    - 体温：{body_temp:.1f}°C
    - 氧气分压：{oxygen_pp:.3f} atm
    - 血氧饱和度：{blood_oxygen:.1f}%
    - 状态：{'💀 已死亡' if is_dead else '✅ 存活'}
    """)
    
    st.markdown("---")
    st.subheader("💬 聊天记录")
    
    # 确保 moss_conversation 存在
    if "moss_conversation" not in st.session_state:
        st.session_state.moss_conversation = []
    
    # 检查是否需要显示自动消息
    if "moss_auto_messages_shown" not in st.session_state:
        st.session_state.moss_auto_messages_shown = []
    
    # 检测高度并自动添加消息
    # 检查当前高度是否在某个消息的触发范围内
    should_show, auto_message, height_key = get_auto_message_at_height(height_km, height_m, env_temp, oxygen_pp)
    
    if should_show and height_key and height_key not in st.session_state.moss_auto_messages_shown:
        # 确保对话历史已初始化
        if len(st.session_state.moss_conversation) == 0:
            # 如果对话历史为空，先初始化system消息
            moss_prompt = get_moss_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
            st.session_state.moss_conversation = [{"role": "system", "content": moss_prompt}]
        
        # 添加自动消息到对话历史
        st.session_state.moss_conversation.append({
            "role": "assistant", 
            "content": auto_message
        })
        st.session_state.moss_auto_messages_shown.append(height_key)
        st.rerun()
    
    # 如果当前高度已经超过了某个目标高度但还没显示过消息，也显示（只在进入页面时）
    # 检查是否错过了2km或4.5km的消息
    if height_km >= 2.2 and "2km" not in st.session_state.moss_auto_messages_shown:
        # 错过了2km消息，现在显示
        should_show, auto_message, height_key = get_auto_message_at_height(2.0, 2000, calculate_temperature(2000), calculate_oxygen_partial_pressure(calculate_pressure(2000)))
        if should_show and height_key:
            if len(st.session_state.moss_conversation) == 0:
                moss_prompt = get_moss_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
                st.session_state.moss_conversation = [{"role": "system", "content": moss_prompt}]
            st.session_state.moss_conversation.append({
                "role": "assistant", 
                "content": auto_message
            })
            st.session_state.moss_auto_messages_shown.append(height_key)
            st.rerun()
    elif height_km >= 4.7 and "4.5km" not in st.session_state.moss_auto_messages_shown:
        # 错过了4.5km消息，现在显示
        should_show, auto_message, height_key = get_auto_message_at_height(4.5, 4500, calculate_temperature(4500), calculate_oxygen_partial_pressure(calculate_pressure(4500)))
        if should_show and height_key:
            if len(st.session_state.moss_conversation) == 0:
                moss_prompt = get_moss_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
                st.session_state.moss_conversation = [{"role": "system", "content": moss_prompt}]
            st.session_state.moss_conversation.append({
                "role": "assistant", 
                "content": auto_message
            })
            st.session_state.moss_auto_messages_shown.append(height_key)
            st.rerun()
    
    # 显示聊天历史（跳过 system 消息）
    if len(st.session_state.moss_conversation) > 1:
        for msg in st.session_state.moss_conversation[1:]:
            if msg["role"] == "user":
                with st.chat_message("user"):
                    st.write(msg["content"])
            elif msg["role"] == "assistant":
                with st.chat_message("assistant", avatar="🤖"):
                    st.write(msg["content"])
    else:
        st.info("💡 还没有聊天记录，在下方输入框开始与 MOSS 对话吧！")
    
    st.markdown("---")
    
    # 用户输入
    user_input = st.chat_input("和 MOSS 聊天...")
    
    if user_input:
        # 确保对话历史已初始化
        if "moss_conversation" not in st.session_state or len(st.session_state.moss_conversation) == 0:
            moss_prompt = get_moss_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
            st.session_state.moss_conversation = [{"role": "system", "content": moss_prompt}]
        
        # 添加用户消息到历史
        st.session_state.moss_conversation.append({
            "role": "user", 
            "content": user_input
        })
        
        # 更新 MOSS prompt（根据最新状态，只更新system消息）
        moss_prompt = get_moss_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
        if len(st.session_state.moss_conversation) > 0:
            st.session_state.moss_conversation[0] = {"role": "system", "content": moss_prompt}
        else:
            st.session_state.moss_conversation = [{"role": "system", "content": moss_prompt}]
        
        # 调用 API 获取 MOSS 回复
        try:
            # 准备API调用用的消息（确保格式正确）
            api_messages = []
            for msg in st.session_state.moss_conversation:
                api_messages.append({"role": msg["role"], "content": msg["content"]})
            
            result = call_zhipu_api(api_messages)
            moss_reply = result['choices'][0]['message']['content']
            
            # 添加 MOSS 回复到历史
            st.session_state.moss_conversation.append({
                "role": "assistant", 
                "content": moss_reply
            })
            
        except Exception as e:
            st.error(f"❌ MOSS 出错了: {e}")
            # 如果API调用失败，保留用户消息，不删除
        
        # 立即刷新页面以显示新消息
        st.rerun()
    
    # 如果模拟正在运行，自动刷新页面以更新状态
    if st.session_state.simulation_running and not st.session_state.death_reason:
        # 减少sleep时间以提高刷新频率
        sleep_time = max(0.01, 0.05 / st.session_state.simulation_speed)
        time.sleep(sleep_time)
        st.rerun()

elif st.session_state.current_page == "与入殓师对话":
    # ========== 入殓师对话专用页面 ==========
    st.title("🕊️ 与入殓师对话")
    st.markdown("**入殓师是一个陪你一起上升的 AI 同伴，充满哲理和人文关怀，优雅而温柔**")
    st.markdown("---")
    
    # 计算当前状态（用于更新入殓师的 prompt）
    # 确保实时计算高度，不依赖可能过时的 current_height
    if st.session_state.simulation_running and st.session_state.start_time:
        elapsed_time = (time.time() - st.session_state.start_time) * st.session_state.simulation_speed
        height_m = elapsed_time * 0.3048
        # 同步更新 current_height，确保两个页面数据一致
        st.session_state.current_height = height_m
    else:
        elapsed_time = 0
        height_m = st.session_state.current_height
    
    height_km = height_m / 1000
    env_temp = calculate_temperature(height_m)
    pressure_atm = calculate_pressure(height_m)
    oxygen_pp = calculate_oxygen_partial_pressure(pressure_atm)
    blood_oxygen = calculate_blood_oxygen_saturation(oxygen_pp)
    body_temp = calculate_body_temperature(env_temp, elapsed_time)
    is_dead = bool(st.session_state.death_reason)
    death_reason = st.session_state.death_reason
    
    # 确保对话历史已初始化
    if "undertaker_conversation" not in st.session_state or len(st.session_state.undertaker_conversation) == 0:
        # 初始化对话历史
        undertaker_prompt = get_undertaker_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
        st.session_state.undertaker_conversation = [{"role": "system", "content": undertaker_prompt}]
        st.session_state.undertaker_initialized = True
        st.session_state.undertaker_last_state_key = f"{height_km:.2f}_{is_dead}_{death_reason}"
    else:
        # 如果对话历史已存在，只更新system消息（如果状态发生重大变化）
        # 检查是否是重大状态变化（死亡状态变化或高度区间变化）
        current_state_key = f"{height_km:.2f}_{is_dead}_{death_reason}"
        last_state_key = st.session_state.get("undertaker_last_state_key", "")
        
        # 解析状态键
        last_parts = last_state_key.split("_") if last_state_key else ["0.0", "False", "None"]
        current_parts = current_state_key.split("_")
        
        last_height_km = float(last_parts[0]) if len(last_parts) > 0 and last_parts[0] else 0.0
        last_is_dead = last_parts[1] if len(last_parts) > 1 else "False"
        current_is_dead = str(is_dead)
        
        # 判断是否需要更新prompt（重大状态变化）
        height_category_changed = (
            (last_height_km < 2.0 and height_km >= 2.0) or
            (last_height_km < 5.0 and height_km >= 5.0) or
            (last_is_dead != current_is_dead)
        )
        
        # 只有在重大状态变化时才更新system prompt
        if height_category_changed or last_state_key == "":
            undertaker_prompt = get_undertaker_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
            # 只更新system消息，保留其他对话历史
            if len(st.session_state.undertaker_conversation) > 0 and st.session_state.undertaker_conversation[0].get("role") == "system":
                st.session_state.undertaker_conversation[0] = {"role": "system", "content": undertaker_prompt}
            else:
                # 如果没有system消息，在开头插入
                st.session_state.undertaker_conversation.insert(0, {"role": "system", "content": undertaker_prompt})
            st.session_state.undertaker_last_state_key = current_state_key
    
    # 显示当前状态信息（在聊天界面顶部）
    st.info(f"""
    📊 **当前模拟状态**：
    - 高度：{height_km:.2f} km
    - 环境温度：{env_temp:.1f}°C
    - 体温：{body_temp:.1f}°C
    - 氧气分压：{oxygen_pp:.3f} atm
    - 血氧饱和度：{blood_oxygen:.1f}%
    - 状态：{'💀 已死亡' if is_dead else '✅ 存活'}
    """)
    
    st.markdown("---")
    st.subheader("💬 聊天记录")
    
    # 确保 undertaker_conversation 存在
    if "undertaker_conversation" not in st.session_state:
        st.session_state.undertaker_conversation = []
    
    # 检查是否需要显示自动消息
    if "undertaker_auto_messages_shown" not in st.session_state:
        st.session_state.undertaker_auto_messages_shown = []
    
    # 检测高度并自动添加消息
    # 检查当前高度是否在某个消息的触发范围内
    should_show, auto_message, height_key = get_auto_message_at_height_undertaker(height_km, height_m, env_temp, oxygen_pp)
    
    if should_show and height_key and height_key not in st.session_state.undertaker_auto_messages_shown:
        # 确保对话历史已初始化
        if len(st.session_state.undertaker_conversation) == 0:
            # 如果对话历史为空，先初始化system消息
            undertaker_prompt = get_undertaker_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
            st.session_state.undertaker_conversation = [{"role": "system", "content": undertaker_prompt}]
        
        # 添加自动消息到对话历史
        st.session_state.undertaker_conversation.append({
            "role": "assistant", 
            "content": auto_message
        })
        st.session_state.undertaker_auto_messages_shown.append(height_key)
        st.rerun()
    
    # 如果当前高度已经超过了某个目标高度但还没显示过消息，也显示（只在进入页面时）
    # 检查是否错过了2km或4.5km的消息
    if height_km >= 2.2 and "2km" not in st.session_state.undertaker_auto_messages_shown:
        # 错过了2km消息，现在显示
        should_show, auto_message, height_key = get_auto_message_at_height_undertaker(2.0, 2000, calculate_temperature(2000), calculate_oxygen_partial_pressure(calculate_pressure(2000)))
        if should_show and height_key:
            if len(st.session_state.undertaker_conversation) == 0:
                undertaker_prompt = get_undertaker_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
                st.session_state.undertaker_conversation = [{"role": "system", "content": undertaker_prompt}]
            st.session_state.undertaker_conversation.append({
                "role": "assistant", 
                "content": auto_message
            })
            st.session_state.undertaker_auto_messages_shown.append(height_key)
            st.rerun()
    elif height_km >= 4.7 and "4.5km" not in st.session_state.undertaker_auto_messages_shown:
        # 错过了4.5km消息，现在显示
        should_show, auto_message, height_key = get_auto_message_at_height_undertaker(4.5, 4500, calculate_temperature(4500), calculate_oxygen_partial_pressure(calculate_pressure(4500)))
        if should_show and height_key:
            if len(st.session_state.undertaker_conversation) == 0:
                undertaker_prompt = get_undertaker_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
                st.session_state.undertaker_conversation = [{"role": "system", "content": undertaker_prompt}]
            st.session_state.undertaker_conversation.append({
                "role": "assistant", 
                "content": auto_message
            })
            st.session_state.undertaker_auto_messages_shown.append(height_key)
            st.rerun()
    
    # 显示聊天历史（跳过 system 消息）
    if len(st.session_state.undertaker_conversation) > 1:
        for msg in st.session_state.undertaker_conversation[1:]:
            if msg["role"] == "user":
                with st.chat_message("user"):
                    st.write(msg["content"])
            elif msg["role"] == "assistant":
                with st.chat_message("assistant", avatar="🕊️"):
                    st.write(msg["content"])
    else:
        st.info("💡 还没有聊天记录，在下方输入框开始与入殓师对话吧！")
    
    st.markdown("---")
    
    # 用户输入
    user_input = st.chat_input("和入殓师聊天...")
    
    if user_input:
        # 确保对话历史已初始化
        if "undertaker_conversation" not in st.session_state or len(st.session_state.undertaker_conversation) == 0:
            undertaker_prompt = get_undertaker_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
            st.session_state.undertaker_conversation = [{"role": "system", "content": undertaker_prompt}]
        
        # 添加用户消息到历史
        st.session_state.undertaker_conversation.append({
            "role": "user", 
            "content": user_input
        })
        
        # 更新入殓师 prompt（根据最新状态，只更新system消息）
        undertaker_prompt = get_undertaker_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
        if len(st.session_state.undertaker_conversation) > 0:
            st.session_state.undertaker_conversation[0] = {"role": "system", "content": undertaker_prompt}
        else:
            st.session_state.undertaker_conversation = [{"role": "system", "content": undertaker_prompt}]
        
        # 调用 API 获取入殓师回复
        try:
            # 准备API调用用的消息（确保格式正确）
            api_messages = []
            for msg in st.session_state.undertaker_conversation:
                api_messages.append({"role": msg["role"], "content": msg["content"]})
            
            result = call_zhipu_api(api_messages)
            undertaker_reply = result['choices'][0]['message']['content']
            
            # 添加入殓师回复到历史
            st.session_state.undertaker_conversation.append({
                "role": "assistant", 
                "content": undertaker_reply
            })
            
        except Exception as e:
            st.error(f"❌ 入殓师出错了: {e}")
            # 如果API调用失败，保留用户消息，不删除
        
        # 立即刷新页面以显示新消息
        st.rerun()
    
    # 如果模拟正在运行，自动刷新页面以更新状态
    if st.session_state.simulation_running and not st.session_state.death_reason:
        # 减少sleep时间以提高刷新频率
        sleep_time = max(0.01, 0.05 / st.session_state.simulation_speed)
        time.sleep(sleep_time)
        st.rerun()

elif st.session_state.current_page == "与未来的我对话":
    # ========== 未来的你对话专用页面 ==========
    st.title("⏰ 与未来的我对话")
    st.markdown("**未来的你是40年后的可能版本，由于时空错乱而出现在这里**")
    st.markdown("---")
    
    # 计算当前状态（用于更新未来自我的 prompt）
    # 确保实时计算高度，不依赖可能过时的 current_height
    if st.session_state.simulation_running and st.session_state.start_time:
        elapsed_time = (time.time() - st.session_state.start_time) * st.session_state.simulation_speed
        height_m = elapsed_time * 0.3048
        # 同步更新 current_height，确保两个页面数据一致
        st.session_state.current_height = height_m
    else:
        elapsed_time = 0
        height_m = st.session_state.current_height
    
    height_km = height_m / 1000
    env_temp = calculate_temperature(height_m)
    pressure_atm = calculate_pressure(height_m)
    oxygen_pp = calculate_oxygen_partial_pressure(pressure_atm)
    blood_oxygen = calculate_blood_oxygen_saturation(oxygen_pp)
    body_temp = calculate_body_temperature(env_temp, elapsed_time)
    is_dead = bool(st.session_state.death_reason)
    death_reason = st.session_state.death_reason
    
    # 确保对话历史已初始化
    if "future_self_conversation" not in st.session_state or len(st.session_state.future_self_conversation) == 0:
        # 初始化对话历史
        future_self_prompt = get_future_self_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
        st.session_state.future_self_conversation = [{"role": "system", "content": future_self_prompt}]
        st.session_state.future_self_initialized = True
        st.session_state.future_self_last_state_key = f"{height_km:.2f}_{is_dead}_{death_reason}"
    else:
        # 如果对话历史已存在，只更新system消息（如果状态发生重大变化）
        # 检查是否是重大状态变化（死亡状态变化或高度区间变化）
        current_state_key = f"{height_km:.2f}_{is_dead}_{death_reason}"
        last_state_key = st.session_state.get("future_self_last_state_key", "")
        
        # 解析状态键
        last_parts = last_state_key.split("_") if last_state_key else ["0.0", "False", "None"]
        current_parts = current_state_key.split("_")
        
        last_height_km = float(last_parts[0]) if len(last_parts) > 0 and last_parts[0] else 0.0
        last_is_dead = last_parts[1] if len(last_parts) > 1 else "False"
        current_is_dead = str(is_dead)
        
        # 判断是否需要更新prompt（重大状态变化）
        height_category_changed = (
            (last_height_km < 2.0 and height_km >= 2.0) or
            (last_height_km < 5.0 and height_km >= 5.0) or
            (last_is_dead != current_is_dead)
        )
        
        # 只有在重大状态变化时才更新system prompt
        if height_category_changed or last_state_key == "":
            future_self_prompt = get_future_self_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
            # 只更新system消息，保留其他对话历史
            if len(st.session_state.future_self_conversation) > 0 and st.session_state.future_self_conversation[0].get("role") == "system":
                st.session_state.future_self_conversation[0] = {"role": "system", "content": future_self_prompt}
            else:
                # 如果没有system消息，在开头插入
                st.session_state.future_self_conversation.insert(0, {"role": "system", "content": future_self_prompt})
            st.session_state.future_self_last_state_key = current_state_key
    
    # 显示当前状态信息（在聊天界面顶部）
    st.info(f"""
    📊 **当前模拟状态**：
    - 高度：{height_km:.2f} km
    - 环境温度：{env_temp:.1f}°C
    - 体温：{body_temp:.1f}°C
    - 氧气分压：{oxygen_pp:.3f} atm
    - 血氧饱和度：{blood_oxygen:.1f}%
    - 状态：{'💀 已死亡' if is_dead else '✅ 存活'}
    """)
    
    st.markdown("---")
    st.subheader("💬 聊天记录")
    
    # 确保 future_self_conversation 存在
    if "future_self_conversation" not in st.session_state:
        st.session_state.future_self_conversation = []
    
    # 检查是否需要显示自动消息
    if "future_self_auto_messages_shown" not in st.session_state:
        st.session_state.future_self_auto_messages_shown = []
    
    # 检测高度并自动添加消息
    # 检查当前高度是否在某个消息的触发范围内
    should_show, auto_message, height_key = get_auto_message_at_height_future_self(height_km, height_m, env_temp, oxygen_pp)
    
    if should_show and height_key and height_key not in st.session_state.future_self_auto_messages_shown:
        # 确保对话历史已初始化
        if len(st.session_state.future_self_conversation) == 0:
            # 如果对话历史为空，先初始化system消息
            future_self_prompt = get_future_self_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
            st.session_state.future_self_conversation = [{"role": "system", "content": future_self_prompt}]
        
        # 添加自动消息到对话历史
        st.session_state.future_self_conversation.append({
            "role": "assistant", 
            "content": auto_message
        })
        st.session_state.future_self_auto_messages_shown.append(height_key)
        st.rerun()
    
    # 如果当前高度已经超过了某个目标高度但还没显示过消息，也显示（只在进入页面时）
    # 检查是否错过了2km或4.5km的消息
    if height_km >= 2.2 and "2km" not in st.session_state.future_self_auto_messages_shown:
        # 错过了2km消息，现在显示
        should_show, auto_message, height_key = get_auto_message_at_height_future_self(2.0, 2000, calculate_temperature(2000), calculate_oxygen_partial_pressure(calculate_pressure(2000)))
        if should_show and height_key:
            if len(st.session_state.future_self_conversation) == 0:
                future_self_prompt = get_future_self_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
                st.session_state.future_self_conversation = [{"role": "system", "content": future_self_prompt}]
            st.session_state.future_self_conversation.append({
                "role": "assistant", 
                "content": auto_message
            })
            st.session_state.future_self_auto_messages_shown.append(height_key)
            st.rerun()
    elif height_km >= 4.7 and "4.5km" not in st.session_state.future_self_auto_messages_shown:
        # 错过了4.5km消息，现在显示
        should_show, auto_message, height_key = get_auto_message_at_height_future_self(4.5, 4500, calculate_temperature(4500), calculate_oxygen_partial_pressure(calculate_pressure(4500)))
        if should_show and height_key:
            if len(st.session_state.future_self_conversation) == 0:
                future_self_prompt = get_future_self_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
                st.session_state.future_self_conversation = [{"role": "system", "content": future_self_prompt}]
            st.session_state.future_self_conversation.append({
                "role": "assistant", 
                "content": auto_message
            })
            st.session_state.future_self_auto_messages_shown.append(height_key)
            st.rerun()
    
    # 显示聊天历史（跳过 system 消息）
    if len(st.session_state.future_self_conversation) > 1:
        for msg in st.session_state.future_self_conversation[1:]:
            if msg["role"] == "user":
                with st.chat_message("user"):
                    st.write(msg["content"])
            elif msg["role"] == "assistant":
                with st.chat_message("assistant", avatar="⏰"):
                    st.write(msg["content"])
    else:
        st.info("💡 还没有聊天记录，在下方输入框开始与未来的我对话吧！")
    
    st.markdown("---")
    
    # 用户输入
    user_input = st.chat_input("和未来的我聊天...")
    
    if user_input:
        # 确保对话历史已初始化
        if "future_self_conversation" not in st.session_state or len(st.session_state.future_self_conversation) == 0:
            future_self_prompt = get_future_self_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
            st.session_state.future_self_conversation = [{"role": "system", "content": future_self_prompt}]
        
        # 添加用户消息到历史
        st.session_state.future_self_conversation.append({
            "role": "user", 
            "content": user_input
        })
        
        # 更新未来自我 prompt（根据最新状态，只更新system消息）
        future_self_prompt = get_future_self_prompt(height_km, env_temp, oxygen_pp, body_temp, blood_oxygen, is_dead, death_reason)
        if len(st.session_state.future_self_conversation) > 0:
            st.session_state.future_self_conversation[0] = {"role": "system", "content": future_self_prompt}
        else:
            st.session_state.future_self_conversation = [{"role": "system", "content": future_self_prompt}]
        
        # 调用 API 获取未来自我回复
        try:
            # 准备API调用用的消息（确保格式正确）
            api_messages = []
            for msg in st.session_state.future_self_conversation:
                api_messages.append({"role": msg["role"], "content": msg["content"]})
            
            result = call_zhipu_api(api_messages)
            future_self_reply = result['choices'][0]['message']['content']
            
            # 添加未来自我回复到历史
            st.session_state.future_self_conversation.append({
                "role": "assistant", 
                "content": future_self_reply
            })
            
        except Exception as e:
            st.error(f"❌ 未来的我出错了: {e}")
            # 如果API调用失败，保留用户消息，不删除
        
        # 立即刷新页面以显示新消息
        st.rerun()
    
    # 如果模拟正在运行，自动刷新页面以更新状态
    if st.session_state.simulation_running and not st.session_state.death_reason:
        # 减少sleep时间以提高刷新频率
        sleep_time = max(0.01, 0.05 / st.session_state.simulation_speed)
        time.sleep(sleep_time)
        st.rerun()

else:
    # ========== 模拟器主页面 ==========
    # ========== 主显示区域 ==========
    # 计算当前状态
    if st.session_state.simulation_running and st.session_state.start_time:
        elapsed_time = (time.time() - st.session_state.start_time) * st.session_state.simulation_speed
        st.session_state.current_height = elapsed_time * 0.3048  # 每秒0.3048米
    else:
        elapsed_time = 0

    height_m = st.session_state.current_height
    height_ft = height_m * 3.28084
    height_km = height_m / 1000

    # 计算物理参数
    env_temp = calculate_temperature(height_m)
    pressure_atm = calculate_pressure(height_m)
    oxygen_pp = calculate_oxygen_partial_pressure(pressure_atm)
    blood_oxygen = calculate_blood_oxygen_saturation(oxygen_pp)
    body_temp = calculate_body_temperature(env_temp, elapsed_time)

    # 检查死亡条件
    is_dead, death_reason, death_details = check_death_conditions(
        height_m, env_temp, oxygen_pp, body_temp, blood_oxygen, elapsed_time
    )

    if is_dead and not st.session_state.death_reason:
        st.session_state.death_reason = death_reason
        st.session_state.simulation_running = False
        # 记录死亡时的程序运行时间
        if st.session_state.real_start_time is not None:
            st.session_state.death_time = time.time() - st.session_state.real_start_time

    # 显示关键指标
    st.markdown("### 📊 实时数据")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        # 当前高度
        height_display = f"{height_km:.3f} km"
        height_delta = f"{height_ft:.0f} 英尺" if height_ft >= 0 else None
        st.metric("📍 当前高度", height_display, delta=height_delta)
    
    with col2:
        # 环境温度
        temp_display = f"{env_temp:.1f}°C"
        if env_temp > -50:
            temp_delta = f"{env_temp*9/5+32:.1f}°F"
        else:
            temp_delta = "极低"
        st.metric("🌡️ 环境温度", temp_display, delta=temp_delta)

    with col3:
        # 氧气分压
        oxy_display = f"{oxygen_pp:.3f} atm"
        if blood_oxygen > 0:
            oxy_delta = f"{blood_oxygen:.1f}% 血氧"
        else:
            oxy_delta = "危险"
        st.metric("💨 氧气分压", oxy_display, delta=oxy_delta)

    with col4:
        # 体温
        body_display = f"{body_temp:.1f}°C"
        if body_temp > 35:
            body_delta = "正常"
        elif body_temp > 28:
            body_delta = "危险"
        else:
            body_delta = "致命"
        st.metric("🫀 体温", body_display, delta=body_delta)

    # 状态显示
    st.markdown("---")

    if st.session_state.death_reason:
        st.error(f"💀 **死亡**：{st.session_state.death_reason}")
        # 重新检查死亡条件以获取详细信息
        is_dead_check, death_reason_check, death_details_check = check_death_conditions(
            height_m, env_temp, oxygen_pp, body_temp, blood_oxygen, elapsed_time
        )
        if death_details_check and st.session_state.death_reason in death_details_check:
            st.warning(death_details_check[st.session_state.death_reason])
        
        # 显示程序运行时间
        if st.session_state.death_time is not None:
            death_time_seconds = st.session_state.death_time
            if death_time_seconds < 60:
                time_display = f"{death_time_seconds:.2f} 秒"
            elif death_time_seconds < 3600:
                minutes = int(death_time_seconds // 60)
                seconds = death_time_seconds % 60
                time_display = f"{minutes} 分 {seconds:.2f} 秒"
            else:
                hours = int(death_time_seconds // 3600)
                minutes = int((death_time_seconds % 3600) // 60)
                seconds = death_time_seconds % 60
                time_display = f"{hours} 小时 {minutes} 分 {seconds:.2f} 秒"
            
            st.info(f"⏱️ **程序运行时间**：从开始模拟到死亡共耗时 {time_display}")
        
        # ========== 死亡后数据报告 ==========
        if len(st.session_state.history) > 0:
            st.markdown("---")
            st.subheader("📊 模拟数据报告")
            
            df = pd.DataFrame(st.session_state.history)
            
            # 死亡时的关键数据
            st.markdown("### 💀 死亡时关键数据")
            col1, col2, col3, col4 = st.columns(4)
            
            with col1:
                st.metric("最终高度", f"{height_km:.3f} km", f"{height_m:.0f} m")
            with col2:
                st.metric("环境温度", f"{env_temp:.1f}°C")
            with col3:
                st.metric("体温", f"{body_temp:.1f}°C")
            with col4:
                st.metric("血氧饱和度", f"{blood_oxygen:.1f}%")
            
            # 数据统计摘要
            st.markdown("### 📈 数据统计摘要")
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("最高高度", f"{df['height'].max()/1000:.3f} km")
                st.metric("最低环境温度", f"{df['env_temp'].min():.1f}°C")
            with col2:
                st.metric("最低体温", f"{df['body_temp'].min():.1f}°C")
                st.metric("最低血氧", f"{df['blood_oxygen'].min():.1f}%")
            with col3:
                st.metric("总模拟时间", f"{df['time'].max():.2f} 秒")
                st.metric("数据点数量", len(df))
            
            # 折线图1：高度 vs 温度
            st.markdown("### 📊 高度 vs 温度变化")
            fig_temp = go.Figure()
            fig_temp.add_trace(go.Scatter(
                x=df["height"] / 1000,
                y=df["env_temp"],
                name="环境温度",
                line=dict(color="blue", width=2),
                mode="lines+markers"
            ))
            fig_temp.add_trace(go.Scatter(
                x=df["height"] / 1000,
                y=df["body_temp"],
                name="体温",
                line=dict(color="red", width=2, dash="dash"),
                mode="lines+markers"
            ))
            # 标记死亡点
            fig_temp.add_trace(go.Scatter(
                x=[height_km],
                y=[body_temp],
                name="死亡点",
                mode="markers",
                marker=dict(size=15, color="red", symbol="x")
            ))
            fig_temp.update_layout(
                title="温度随高度变化",
                xaxis_title="高度 (km)",
                yaxis_title="温度 (°C)",
                hovermode="x unified",
                height=400
            )
            st.plotly_chart(fig_temp, use_container_width=True)
            
            # 折线图2：高度 vs 氧气相关指标
            st.markdown("### 📊 高度 vs 氧气指标变化")
            fig_oxygen = go.Figure()
            fig_oxygen.add_trace(go.Scatter(
                x=df["height"] / 1000,
                y=df["oxygen_pp"],
                name="氧气分压 (atm)",
                line=dict(color="green", width=2),
                mode="lines+markers",
                yaxis="y"
            ))
            fig_oxygen.add_trace(go.Scatter(
                x=df["height"] / 1000,
                y=df["blood_oxygen"],
                name="血氧饱和度 (%)",
                line=dict(color="orange", width=2),
                mode="lines+markers",
                yaxis="y2"
            ))
            # 标记死亡点
            fig_oxygen.add_trace(go.Scatter(
                x=[height_km],
                y=[blood_oxygen],
                name="死亡点",
                mode="markers",
                marker=dict(size=15, color="red", symbol="x"),
                yaxis="y2"
            ))
            fig_oxygen.update_layout(
                title="氧气指标随高度变化",
                xaxis_title="高度 (km)",
                yaxis=dict(title="氧气分压 (atm)", side="left"),
                yaxis2=dict(title="血氧饱和度 (%)", side="right", overlaying="y"),
                hovermode="x unified",
                height=400
            )
            st.plotly_chart(fig_oxygen, use_container_width=True)
            
            # 折线图3：时间 vs 关键指标
            st.markdown("### 📊 时间 vs 关键指标变化")
            fig_time = go.Figure()
            fig_time.add_trace(go.Scatter(
                x=df["time"],
                y=df["height"] / 1000,
                name="高度 (km)",
                line=dict(color="purple", width=2),
                mode="lines+markers"
            ))
            fig_time.add_trace(go.Scatter(
                x=df["time"],
                y=df["body_temp"],
                name="体温 (°C)",
                line=dict(color="red", width=2, dash="dash"),
                mode="lines+markers",
                yaxis="y2"
            ))
            fig_time.add_trace(go.Scatter(
                x=df["time"],
                y=df["blood_oxygen"],
                name="血氧饱和度 (%)",
                line=dict(color="orange", width=2, dash="dot"),
                mode="lines+markers",
                yaxis="y2"
            ))
            # 标记死亡点
            fig_time.add_trace(go.Scatter(
                x=[df["time"].max()],
                y=[height_km],
                name="死亡点",
                mode="markers",
                marker=dict(size=15, color="red", symbol="x")
            ))
            fig_time.update_layout(
                title="关键指标随时间变化",
                xaxis_title="时间 (秒)",
                yaxis=dict(title="高度 (km)", side="left"),
                yaxis2=dict(title="体温 (°C) / 血氧饱和度 (%)", side="right", overlaying="y"),
                hovermode="x unified",
                height=400
            )
            st.plotly_chart(fig_time, use_container_width=True)
            
            # 完整数据表格
            st.markdown("### 📋 完整数据表格")
            # 创建显示用的DataFrame
            df_display = pd.DataFrame({
                "时间 (秒)": df["time"],
                "高度 (km)": df["height"] / 1000,
                "环境温度 (°C)": df["env_temp"],
                "体温 (°C)": df["body_temp"],
                "气压 (atm)": df["pressure"],
                "氧气分压 (atm)": df["oxygen_pp"],
                "血氧饱和度 (%)": df["blood_oxygen"]
            })
            
            # 添加状态列，标记最后一行（死亡点）
            status_list = ["存活"] * (len(df_display) - 1) + ["💀 死亡"]
            df_display["状态"] = status_list
            
            st.dataframe(
                df_display.round(3),
                use_container_width=True,
                height=400
            )
            
            # 导出数据选项
            st.markdown("### 💾 导出数据")
            csv = df_display.to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                label="📥 下载完整数据 (CSV)",
                data=csv,
                file_name=f"simulation_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv"
            )
        
        st.session_state.simulation_running = False
    elif height_m > 0:
        # 警告系统（只关注冻死和窒息）
        warnings = []
        if body_temp < 32:
            warnings.append("⚠️ 体温过低，有冻死风险（体温 < 28°C 将死亡）")
        if oxygen_pp < 0.12:
            warnings.append("⚠️ 氧气不足，呼吸困难（氧气分压 < 0.08 atm 将窒息）")
        
        if warnings:
            for warning in warnings:
                st.warning(warning)
        else:
            st.success("✅ 当前状态：存活")
    else:
        st.info("ℹ️ 在左侧栏点击开始按钮开始模拟")

    # ========== p5.js 可视化 ==========
    def create_p5js_visualization(height_m, height_km, env_temp, body_temp, oxygen_pp, blood_oxygen, is_dead, death_reason):
        """创建 p5.js 可视化 HTML"""
        
        # 将数据传递给 JavaScript
        data = {
            "height_m": height_m,
            "height_km": height_km,
            "env_temp": env_temp,
            "body_temp": body_temp,
            "oxygen_pp": oxygen_pp,
            "blood_oxygen": blood_oxygen,
            "is_dead": is_dead,
            "death_reason": death_reason or ""
        }
        
        html_code = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.7.0/p5.min.js"></script>
        <style>
            body {{
                margin: 0;
                padding: 0;
                overflow: hidden;
            }}
        </style>
    </head>
    <body>
        <script>
            // 从 Python 传递的数据
            const simData = {json.dumps(data)};
            
            let personY;
            let clouds = [];
            let particles = [];
            
            function setup() {{
                createCanvas(800, 600);
                personY = height - 100; // 初始位置在底部
                
                // 初始化云朵
                for (let i = 0; i < 20; i++) {{
                    clouds.push({{
                        x: random(width),
                        y: random(height),
                        size: random(30, 80),
                        speed: random(0.5, 2)
                    }});
                }}
            }}
            
            function draw() {{
                // 根据高度计算背景颜色（从蓝色渐变到深蓝/黑色）
                let bgColor = map(simData.height_km, 0, 12, 100, 0);
                background(bgColor, bgColor + 50, bgColor + 100);
                
                // 绘制天空渐变
                for (let i = 0; i < height; i++) {{
                    let inter = map(i, 0, height, 0, 1);
                    let c = lerpColor(
                        color(135, 206, 250), // 天蓝色
                        color(0, 0, 50),      // 深蓝色
                        inter
                    );
                    stroke(c);
                    line(0, i, width, i);
                }}
                
                // 绘制云朵
                for (let cloud of clouds) {{
                    cloud.y -= cloud.speed * (simData.height_km / 10 + 0.1);
                    if (cloud.y < -cloud.size) {{
                        cloud.y = height + cloud.size;
                        cloud.x = random(width);
                    }}
                    
                    fill(255, 255, 255, 150);
                    noStroke();
                    ellipse(cloud.x, cloud.y, cloud.size, cloud.size * 0.6);
                    ellipse(cloud.x - cloud.size * 0.3, cloud.y, cloud.size * 0.8, cloud.size * 0.5);
                    ellipse(cloud.x + cloud.size * 0.3, cloud.y, cloud.size * 0.8, cloud.size * 0.5);
                }}
                
                // 计算人物位置（从底部向上移动）
                // 最大高度12km对应画布顶部
                personY = map(simData.height_km, 0, 12, height - 100, 50);
                personY = constrain(personY, 50, height - 100);
                
                // 绘制高度标尺
                drawHeightScale();
                
                // 绘制人物
                drawPerson(personY);
                
                // 绘制状态指示器
                drawStatusIndicators();
                
                // 如果死亡，显示死亡效果
                if (simData.is_dead) {{
                    drawDeathEffect();
                }}
            }}
            
            function drawHeightScale() {{
                // 绘制高度标尺
                stroke(255, 200);
                strokeWeight(1);
                for (let i = 0; i <= 12; i += 1) {{
                    let y = map(i, 0, 12, height - 50, 50);
                    line(50, y, 70, y);
                    fill(255);
                    noStroke();
                    textAlign(RIGHT);
                    textSize(12);
                    text(i + "km", 45, y + 4);
                }}
                
                // 当前高度标记
                stroke(255, 0, 0);
                strokeWeight(2);
                line(50, personY, width - 200, personY);
                fill(255, 0, 0);
                textAlign(LEFT);
                textSize(14);
                text("当前: " + simData.height_km.toFixed(2) + " km", 75, personY - 5);
            }}
            
            function drawPerson(y) {{
                push();
                translate(width / 2, y);
                
                // 根据体温改变颜色
                let tempColor = map(simData.body_temp, 20, 37, 0, 255);
                tempColor = constrain(tempColor, 0, 255);
                fill(255, tempColor, tempColor);
                
                // 身体
                ellipse(0, 0, 40, 60);
                
                // 头部
                fill(255, 220, 177);
                ellipse(0, -40, 30, 30);
                
                // 眼睛
                fill(0);
                ellipse(-8, -45, 5, 5);
                ellipse(8, -45, 5, 5);
                
                // 根据血氧饱和度改变眼睛状态
                if (simData.blood_oxygen < 70) {{
                    // 眼睛半闭
                    fill(255, 220, 177);
                    ellipse(-8, -44, 5, 2);
                    ellipse(8, -44, 5, 2);
                }}
                
                // 手臂（表示挣扎或无力）
                stroke(255, tempColor, tempColor);
                strokeWeight(3);
                if (simData.blood_oxygen < 80) {{
                    // 手臂下垂
                    line(-20, -10, -25, 20);
                    line(20, -10, 25, 20);
                }} else {{
                    // 正常姿势
                    line(-20, -10, -30, 10);
                    line(20, -10, 30, 10);
                }}
                
                // 如果死亡，显示X标记
                if (simData.is_dead) {{
                    stroke(255, 0, 0);
                    strokeWeight(3);
                    line(-15, -50, 15, -20);
                    line(15, -50, -15, -20);
                }}
                
                pop();
            }}
            
            function drawStatusIndicators() {{
                // 在右侧绘制状态条
                let x = width - 180;
                let y = 50;
                let barWidth = 150;
                let barHeight = 15;
                
                // 体温指示器
                fill(255, 100, 100);
                rect(x, y, barWidth, barHeight);
                let tempPercent = map(simData.body_temp, 20, 37, 0, 100);
                tempPercent = constrain(tempPercent, 0, 100);
                fill(255, 0, 0);
                rect(x, y, barWidth * (tempPercent / 100), barHeight);
                fill(255);
                textAlign(LEFT);
                textSize(10);
                text("体温: " + simData.body_temp.toFixed(1) + "°C", x, y - 5);
                
                // 血氧指示器
                y += 30;
                fill(100, 100, 255);
                rect(x, y, barWidth, barHeight);
                fill(0, 0, 255);
                rect(x, y, barWidth * (simData.blood_oxygen / 100), barHeight);
                fill(255);
                text("血氧: " + simData.blood_oxygen.toFixed(1) + "%", x, y - 5);
                
                // 氧气分压指示器
                y += 30;
                fill(100, 255, 100);
                rect(x, y, barWidth, barHeight);
                let oxyPercent = map(simData.oxygen_pp, 0, 0.21, 0, 100);
                oxyPercent = constrain(oxyPercent, 0, 100);
                fill(0, 255, 0);
                rect(x, y, barWidth * (oxyPercent / 100), barHeight);
                fill(255);
                text("氧气: " + simData.oxygen_pp.toFixed(3) + " atm", x, y - 5);
                
                // 环境温度指示器
                y += 30;
                let tempColor = map(simData.env_temp, -60, 15, 0, 255);
                tempColor = constrain(tempColor, 0, 255);
                fill(255 - tempColor, tempColor, 255);
                rect(x, y, barWidth, barHeight);
                fill(255);
                text("环境: " + simData.env_temp.toFixed(1) + "°C", x, y - 5);
                
                // 死亡信息
                if (simData.is_dead) {{
                    y += 40;
                    fill(255, 0, 0);
                    textSize(16);
                    textAlign(CENTER);
                    text("💀 " + simData.death_reason, width / 2, y);
                }}
            }}
            
            function drawDeathEffect() {{
                // 死亡时的视觉效果
                fill(255, 0, 0, 50);
                noStroke();
                ellipse(width / 2, personY, 200, 200);
                
                // 闪烁效果
                if (frameCount % 30 < 15) {{
                    fill(255, 0, 0, 100);
                    rect(0, 0, width, height);
                }}
            }}
        </script>
    </body>
    </html>
    """
        return html_code

    # 显示 p5.js 可视化
    if st.session_state.simulation_running or height_m > 0:
        st.markdown("---")
        st.subheader("🎨 实时可视化")
        
        # 创建 p5.js 可视化
        p5_html = create_p5js_visualization(
            height_m, height_km, env_temp, body_temp, 
            oxygen_pp, blood_oxygen, 
            bool(st.session_state.death_reason), 
            st.session_state.death_reason
        )
        
        # 使用 Streamlit 组件嵌入 HTML
        st.components.v1.html(p5_html, height=600, scrolling=False)

    # 实时更新
    if st.session_state.simulation_running and not st.session_state.death_reason:
        # 记录历史数据
        st.session_state.history.append({
            "time": elapsed_time,
            "height": height_m,
            "env_temp": env_temp,
            "body_temp": body_temp,
            "pressure": pressure_atm,
            "oxygen_pp": oxygen_pp,
            "blood_oxygen": blood_oxygen
        })
        
        # 限制历史记录数量
        if len(st.session_state.history) > 1000:
            st.session_state.history = st.session_state.history[-1000:]
        
        # 自动刷新（模拟实时更新）
        # 减少sleep时间以提高刷新频率，但不要太小以免CPU占用过高
        sleep_time = max(0.01, 0.05 / st.session_state.simulation_speed)
        time.sleep(sleep_time)
        st.rerun()

    # ========== 数据可视化 ==========
    if len(st.session_state.history) > 1:
        st.markdown("---")
        st.subheader("📈 实时数据图表")
        
        df = pd.DataFrame(st.session_state.history)
        
        # 创建图表
        fig = go.Figure()
        
        # 温度曲线
        fig.add_trace(go.Scatter(
            x=df["height"] / 1000,
            y=df["env_temp"],
            name="环境温度",
            line=dict(color="blue", width=2),
            yaxis="y"
        ))
        
        fig.add_trace(go.Scatter(
            x=df["height"] / 1000,
            y=df["body_temp"],
            name="体温",
            line=dict(color="red", width=2, dash="dash"),
            yaxis="y"
        ))
        
        # 氧气曲线（次坐标轴）
        fig.add_trace(go.Scatter(
            x=df["height"] / 1000,
            y=df["blood_oxygen"],
            name="血氧饱和度 (%)",
            line=dict(color="green", width=2),
            yaxis="y2"
        ))
        
        # 更新布局
        fig.update_layout(
            title="高度 vs 温度 & 血氧饱和度",
            xaxis_title="高度 (km)",
            yaxis=dict(title="温度 (°C)", side="left"),
            yaxis2=dict(title="血氧饱和度 (%)", side="right", overlaying="y"),
            hovermode="x unified",
            height=400
        )
        
        st.plotly_chart(fig, use_container_width=True)
        
        # 数据表格
        with st.expander("📊 查看详细数据"):
            st.dataframe(
                df.tail(20)[["time", "height", "env_temp", "body_temp", "oxygen_pp", "blood_oxygen"]].round(2),
                use_container_width=True
            )

    # ========== 理论分析 ==========
    st.markdown("---")
    st.subheader("🔬 理论分析")

    # 理论分析部分始终显示
    st.markdown("### 📖 关于死亡顺序的科学分析")

    st.markdown("""
#### 你会先被冻死还是因窒息而死？

**答案：很可能是先因缺氧/窒息而死，而不是冻死。**

##### 原因分析：

1. **窒息发生更快**：
   - 在约 **5-6公里** 高度，氧气分压降至危险水平（约 0.10-0.11 atm）
   - 当氧气分压 < 0.10 atm 或血氧饱和度 < 70% 时，人体无法维持正常呼吸，导致窒息死亡
   - 人体对缺氧的耐受性较差，几分钟内就会失去意识

2. **冻死需要更长时间**：
   - 体温下降是渐进过程，在温和环境下（> 5°C）体温每小时仅下降约 0.05°C
   - 即使环境温度很低，体温也需要数小时才会降至致命水平（< 28°C）
   - 在缺氧导致死亡之前，体温可能还没降到致命程度

3. **实际高度分析**：
   - **1-2公里**：环境温度约 8-12°C，氧气分压约 0.18-0.20 atm，完全安全
   - **3-4公里**：开始出现轻微缺氧症状，氧气分压约 0.15-0.18 atm，体温基本正常
   - **5-6公里**：严重缺氧，氧气分压降至 0.10-0.11 atm，血氧饱和度 < 70%，**因窒息死亡**
   - **8公里以上**：氧气极度稀薄（< 0.09 atm），必然窒息
   - **10公里以上**：环境温度约 -50°C，但如果上升到这个高度，通常已经因缺氧死亡

##### 结论：

在每秒1英尺的上升速度下，**最可能的死因是窒息**，发生在约 **5-6公里** 高度
（氧气分压 < 0.10 atm 或血氧饱和度 < 70%），此时环境温度可能只有 -18°C 到 -24°C 左右，
还不足以快速冻死人体（冻死需要体温降至 28°C 以下，需要数小时）。
    """)

    st.markdown("---")
    st.caption("⚠️ 本模拟器基于标准大气模型和简化生理模型，仅供参考，不构成医学建议。")



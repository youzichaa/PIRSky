#!/bin/bash
# =============================================================================
# 批量运行 Go 程序 - 每个任务内部循环NUM_ITERATIONS次，结果合并到同一文件
# 用法: ./run_go_batch.sh
# =============================================================================

# 配置参数
GO_PROGRAM="tutorial_griddb_metrics2.go"
INTPUT_DIR="/SkyDB"
OUTPUT_DIR="/SkyDB/cost2"
NUM_ITERATIONS=10

Data_range=1000

# 参数列表
D_VALUES=(1)
# N_VALUES=(1000 3000 5000 7000 9000)
N_VALUES=(10000 100000)
TYPE_VALUES=(CORR INDE ANTI HOS STR VEH)
# T_VALUES=(1 2)
PE_VALUES=("1_75_50" "2_95_90")
# PE_VALUES=("1_80_60" "2_90_80")
# PE_VALUES=("1_75_50")

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# =============================================================================
# 函数定义
# =============================================================================

# 打印带颜色的消息
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

# 创建输出目录
create_output_dir() {
    if [ ! -d "$OUTPUT_DIR" ]; then
        mkdir -p "$OUTPUT_DIR"
        print_info "创建输出目录: $OUTPUT_DIR"
    fi
}

# 检查Go程序是否存在
check_go_program() {
    if [ ! -f "$GO_PROGRAM" ]; then
        print_error "Go程序不存在: $GO_PROGRAM"
        exit 1
    fi
}

# 构建数据库文件名
build_db_name() {
    local d=$1
    local n=$2
    local type=$3
    local pe=$4
    echo "skyagg_griddb_${d}_${n}_${type}_mode${pe}.pir.db"
}

# 构建元数据文件名
build_meta_name() {
    local d=$1
    local n=$2
    local type=$3
    local pe=$4
    echo "skyagg_griddb_${d}_${n}_${type}_mode${pe}.pir.meta.json"
}

# 构建指标输出文件名
build_metrics_name() {
    local d=$1
    local n=$2
    local type=$3
    local pe=$4
    echo "piano_metrics_${d}_${n}_${type}_mode${pe}.json"
}

# 构建日志文件名
build_log_name() {
    local d=$1
    local n=$2
    local type=$3
    local pe=$4
    echo "run_${d}_${n}_${type}_mode${pe}.log"
}

gen_int64() {
    od -An -N8 -td8 < /dev/urandom | tr -d ' '
}

gen_random_index() {
    local num=$(od -An -N4 -tu4 < /dev/urandom | tr -d ' ')
    echo $((num % (Data_range * Data_range)))
}

# 运行单个Go命令
run_single_iteration() {
    local db_path=$1
    local meta_path=$2
    local index=$3
    local metrics_path=$4
    local log_file=$5
    local iter_num=$6
    
    local cmd="go run $GO_PROGRAM -db $db_path -meta $meta_path -index $index -metrics-json $metrics_path"
    
    {
        echo "================================================================================"
        echo "迭代 #$iter_num"
        echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "索引: $index"
        echo "命令: $cmd"
        echo "================================================================================"
        
        $cmd 2>&1
        
        EXIT_CODE=$?
        
        echo ""
        echo "退出码: $EXIT_CODE"
        echo "================================================================================"
        
        return $EXIT_CODE
        
    } >> "$log_file" 2>&1
}

# =============================================================================
# 合并多个 JSON 文件
# =============================================================================

merge_json_files() {
    local output_file=$1
    shift
    local json_files=("$@")
    
    # 检查是否有文件
    if [ ${#json_files[@]} -eq 0 ]; then
        echo "[]" > "$output_file"
        return 0
    fi
    
    # 使用 Python 合并 JSON，通过参数传递文件列表
    python3 -c "
import json
import sys

output_file = sys.argv[1]
json_files = sys.argv[2:]

combined = []
for f in json_files:
    try:
        with open(f, 'r') as file:
            data = json.load(file)
            if isinstance(data, list):
                combined.extend(data)
            else:
                combined.append(data)
    except Exception as e:
        print(f'Error reading {f}: {e}', file=sys.stderr)

with open(output_file, 'w') as out:
    json.dump(combined, out, indent=2)
" "$output_file" "${json_files[@]}" 2>/dev/null
}

# =============================================================================
# 运行单个任务 (内部循环 NUM_ITERATIONS 次)
# =============================================================================

run_go_command() {
    local d=$1
    local n=$2
    local type=$3
    local pe=$4

    
    # 构建文件名
    DB_NAME=$(build_db_name $d $n $type $pe)
    META_NAME=$(build_meta_name $d $n $type $pe)
    METRICS_NAME=$(build_metrics_name $d $n $type $pe)
    LOG_NAME=$(build_log_name $d $n $type $pe)
    
    # 完整路径
    DB_PATH="$INTPUT_DIR/$DB_NAME"
    META_PATH="$INTPUT_DIR/$META_NAME"
    METRICS_PATH="$OUTPUT_DIR/$METRICS_NAME"
    LOG_PATH="$OUTPUT_DIR/$LOG_NAME"
    
    # 检查数据库文件是否存在
    if [ ! -f "$DB_PATH" ]; then
        print_warning "跳过: 数据库文件不存在 $DB_PATH"
        return 1
    fi
    
    # 检查元数据文件是否存在
    if [ ! -f "$META_PATH" ]; then
        print_warning "跳过: 元数据文件不存在 $META_PATH"
        return 1
    fi
    
    # 创建临时目录存放每次迭代的 JSON
    TEMP_DIR="$OUTPUT_DIR/temp_${d}_${n}_${type}_${t}_${pe}"
    mkdir -p "$TEMP_DIR"

    # 打印信息
    echo ""
    echo "================================================================================"
    echo "运行: d=$d, n=$n, type=$type, mode=$pe ($NUM_ITERATIONS 次迭代)"
    echo "================================================================================"
    echo "数据库: $DB_NAME"
    echo "元数据: $META_NAME"
    echo "输出:   $METRICS_NAME"
    echo "日志:   $LOG_NAME"
    echo "命令:   $CMD"
    echo "-------------------------------------------------------------------------------"
    
    # 执行命令并保存日志
    {
        echo "================================================================================"
        echo "任务开始: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "参数: d=$d, n=$n, type=$type, mode=$pe"
        echo "迭代次数: $NUM_ITERATIONS"
        echo "数据库: $DB_PATH"
        echo "元数据: $META_PATH"
        echo "输出: $METRICS_PATH"
        echo "================================================================================"
        echo ""
    } > "$LOG_PATH"
    
    # 统计变量
    local success_count=0
    local temp_json_files=()
    
    
    # 循环运行 NUM_ITERATIONS 次
    for iter in $(seq 1 $NUM_ITERATIONS); do
        # 生成随机索引 (每次不同)
        local random_index=$(gen_random_index)
        
        # 临时 JSON 文件
        TEMP_JSON="$TEMP_DIR/iter_${iter}.json"
        temp_json_files+=("$TEMP_JSON")
        
        echo "  迭代 $iter/$NUM_ITERATIONS: 索引=$random_index"
        
        # 运行单次迭代
        run_single_iteration "$DB_PATH" "$META_PATH" "$random_index" "$TEMP_JSON" "$LOG_PATH" "$iter"
        
        if [ $? -eq 0 ]; then
            success_count=$((success_count + 1))
        else
            print_warning "  迭代 $iter 失败"
        fi
    done
    
    # 合并所有 JSON 文件
    if [ ${#temp_json_files[@]} -gt 0 ]; then
        print_info "  合并 ${#temp_json_files[@]} 个 JSON 文件..."
        merge_json_files "$METRICS_PATH" "${temp_json_files[@]}"
    else
        echo "[]" > "$METRICS_PATH"
    fi
    
    # 清理临时文件
    rm -rf "$TEMP_DIR"
    
    # 添加合并信息到日志
    {
        echo ""
        echo "================================================================================"
        echo "任务完成: $(date '+%Y-%m-%d %H:%M:%S')"
        echo "成功迭代: $success_count / $NUM_ITERATIONS"
        echo "输出文件: $METRICS_PATH"
        echo "================================================================================"
    } >> "$LOG_PATH"
    
    # 打印结果
    if [ $success_count -eq $NUM_ITERATIONS ]; then
        print_success "完成: d=$d, n=$n, type=$type, mode=$pe ($success_count/$NUM_ITERATIONS)"
        return 0
    elif [ $success_count -gt 0 ]; then
        print_warning "部分完成: d=$d, n=$n, type=$type, mode=$pe ($success_count/$NUM_ITERATIONS)"
        return 0
    else
        print_error "失败: d=$d, n=$n, type=$type, mode=$pe ($success_count/$NUM_ITERATIONS)"
        return 1
    fi
}

# =============================================================================
# 主函数
# =============================================================================

main() {
    echo "================================================================================"
    echo "批量运行 Go 程序 (每个任务循环 $NUM_ITERATIONS 次)"
    echo "================================================================================"
    echo "程序: $GO_PROGRAM"
    echo "输出目录: $OUTPUT_DIR"
    echo "迭代次数: $NUM_ITERATIONS"
    echo ""
    
    # 初始化
    create_output_dir
    check_go_program
    
    # 统计变量
    TOTAL=0
    SUCCESS=0
    FAILED=0
    SKIPPED=0
    
    # 记录失败的任务
    FAILED_TASKS=""
    
    # 计算总任务数
    for d in "${D_VALUES[@]}"; do
        for n in "${N_VALUES[@]}"; do
            for type in "${TYPE_VALUES[@]}"; do
                for pe in "${PE_VALUES[@]}"; do
                    TOTAL=$((TOTAL + 1))
                done
            done
        done
    done
    
    print_info "总任务数: $TOTAL"
    echo ""
    
    # 开始时间
    START_TIME=$(date +%s)
    
    # 循环执行
    TASK_ID=0
    for d in "${D_VALUES[@]}"; do
        for n in "${N_VALUES[@]}"; do
            for type in "${TYPE_VALUES[@]}"; do
                for pe in "${PE_VALUES[@]}"; do
                    TASK_ID=$((TASK_ID + 1))
                    echo ""
                    print_info "[$TASK_ID/$TOTAL] 开始处理..."
                        
                    run_go_command $d $n $type $pe
                        
                    if [ $? -eq 0 ]; then
                         SUCCESS=$((SUCCESS + 1))
                    elif [ $? -eq 1 ]; then
                        SKIPPED=$((SKIPPED + 1))
                    else
                        FAILED=$((FAILED + 1))
                        FAILED_TASKS="$FAILED_TASKS\n  d=$d, n=$n, type=$type, mode=$pe"
                    fi
                done
            done
        done
    done
    
    # 结束时间
    END_TIME=$(date +%s)
    DURATION=$((END_TIME - START_TIME))
    
    # 打印总结
    echo ""
    echo "================================================================================"
    echo "执行完成!"
    echo "================================================================================"
    echo "总任务数: $TOTAL"
    echo "成功:     $SUCCESS"
    echo "跳过:     $SKIPPED (文件不存在)"
    echo "失败:     $FAILED"
    echo "耗时:     $((DURATION / 60)) 分钟 $((DURATION % 60)) 秒"
    echo ""
    
    if [ $FAILED -gt 0 ]; then
        echo "失败的任务:"
        echo -e "$FAILED_TASKS"
        echo ""
    fi
    
    echo "日志文件保存在: $OUTPUT_DIR/"
    echo "  - 每个任务一个 JSON 文件 (包含 $NUM_ITERATIONS 次结果)"
    echo "================================================================================"
    
    # 返回失败数量
    if [ $FAILED -gt 0 ]; then
        exit 1
    else
        exit 0
    fi
}

# 运行主函数
main